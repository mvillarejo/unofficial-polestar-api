"""Live subscriptions layered on top of the poll.

Streams are the primary responsiveness mechanism, but the poll is still the
freshness guarantee underneath them — a dead stream is a non-event that the
next poll quietly recovers from, rather than something a watchdog has to
detect.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.polestar.const import (
    CONF_ENABLE_STREAMS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
)
from custom_components.polestar.coordinator import STREAM_METHODS
from polestar_api.models.battery import Battery, ChargingStatus


async def test_streams_are_on_by_default(coordinator) -> None:
    """The default: streams accelerate polling rather than replacing it."""
    assert coordinator.streams_enabled


async def test_no_streams_started_when_vehicle_has_no_stream_methods(
    coordinator, mock_vehicle
) -> None:
    """mock_vehicle deliberately has no stream_* attrs; nothing should start."""
    await coordinator.async_start_streams()
    assert not coordinator._stream_tasks


@pytest.fixture
def streaming_vehicle(mock_vehicle):
    """A vehicle whose stream_* methods yield one value then block."""
    updates: dict[str, asyncio.Queue] = {}

    def _make(attr: str):
        queue: asyncio.Queue = asyncio.Queue()
        updates[attr] = queue

        async def _stream():
            while True:
                yield await queue.get()

        return _stream

    for attr in STREAM_METHODS:
        setattr(mock_vehicle, STREAM_METHODS[attr], _make(attr))
    mock_vehicle.stream_updates = updates
    return mock_vehicle


@pytest.fixture
async def streaming_setup(
    hass: HomeAssistant, mock_api, streaming_vehicle
) -> MockConfigEntry:
    """An entry with a vehicle that actually supports streaming."""
    entry = MockConfigEntry(
        domain="polestar",
        unique_id="LPSVSEDEEPL000001",
        version=2,
        data={"email": "u@example.com", "password": "p", "vin": "LPSVSEDEEPL000001"},
        options={
            CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
            CONF_ENABLE_STREAMS: True,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.polestar.PolestarApi", return_value=mock_api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_enabled_streams_start_for_every_streamable_attr(
    streaming_setup, hass: HomeAssistant
) -> None:
    coordinator = next(iter(streaming_setup.runtime_data.coordinators.values()))
    assert set(coordinator._stream_tasks) == set(STREAM_METHODS)


async def test_stream_value_reaches_entity_state(
    streaming_setup, hass: HomeAssistant, streaming_vehicle
) -> None:
    """A pushed value updates the shared snapshot and the entity."""
    coordinator = next(iter(streaming_setup.runtime_data.coordinators.values()))
    assert coordinator.data.battery.charge_level == 62

    await streaming_vehicle.stream_updates["battery"].put(
        Battery(charge_level=71, charging_status=ChargingStatus.CHARGING)
    )
    await hass.async_block_till_done()

    assert coordinator.data.battery.charge_level == 71
    assert hass.states.get("sensor.polestar_4_test123_battery_level").state == "71"


async def test_finished_stream_is_restarted_by_the_next_poll(
    streaming_setup, hass: HomeAssistant
) -> None:
    """Recovery rides on the poll cycle instead of a bespoke watchdog."""
    coordinator = next(iter(streaming_setup.runtime_data.coordinators.values()))
    task = coordinator._stream_tasks["battery"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert coordinator._stream_tasks["battery"].done()

    await coordinator.async_refresh()

    assert not coordinator._stream_tasks["battery"].done()
    assert coordinator._stream_tasks["battery"] is not task


async def test_streams_are_cancelled_on_unload(
    streaming_setup, hass: HomeAssistant
) -> None:
    coordinator = next(iter(streaming_setup.runtime_data.coordinators.values()))
    tasks = list(coordinator._stream_tasks.values())
    assert tasks

    assert await hass.config_entries.async_unload(streaming_setup.entry_id)
    await hass.async_block_till_done()

    assert all(task.done() for task in tasks)


async def test_polling_still_runs_with_streams_enabled(
    streaming_setup, hass: HomeAssistant, streaming_vehicle
) -> None:
    """Streams never replace the poll — it is still the freshness guarantee."""
    coordinator = next(iter(streaming_setup.runtime_data.coordinators.values()))
    streaming_vehicle.get_battery.reset_mock()

    await coordinator.async_refresh()

    streaming_vehicle.get_battery.assert_called_once()
