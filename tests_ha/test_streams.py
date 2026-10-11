"""Live subscriptions layered on top of the poll.

Streams are the responsiveness mechanism for the few attributes worth their
price (the server closes each one after ~120s, so an open stream costs ~720
calls a day). Battery, exterior and climate stream whenever streams are on;
location only while the car is active. The poll is still the freshness
guarantee underneath them — a dead stream is a non-event that the next poll
quietly recovers from, rather than something a watchdog has to detect.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.polestar.const import (
    CONF_ENABLE_STREAMS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    STREAM_MIN_RESUBSCRIBE_INTERVAL,
)
from custom_components.polestar.coordinator import (
    ACTIVE_ONLY_STREAMS,
    ALWAYS_ON_STREAMS,
    STREAM_METHODS,
)
from custom_components.polestar.diagnostics import async_get_config_entry_diagnostics
from polestar_api.models.battery import Battery, ChargingStatus
from polestar_api.models.exterior import CentralLockStatus, ExteriorStatus, LockStatus

IDLE_BATTERY = Battery(charge_level=62, charging_status=ChargingStatus.IDLE, range_km=310)
LOCKED = ExteriorStatus(central_lock=CentralLockStatus(lock_status=LockStatus.LOCKED))
UNLOCKED = ExteriorStatus(central_lock=CentralLockStatus(lock_status=LockStatus.UNLOCKED))


def _running(coordinator) -> set[str]:
    return {attr for attr, task in coordinator._stream_tasks.items() if not task.done()}


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


@pytest.fixture
async def idle_streaming_setup(
    hass: HomeAssistant, mock_api, streaming_vehicle
) -> MockConfigEntry:
    """Like streaming_setup, but the car is parked, locked and not charging."""
    streaming_vehicle.get_battery.return_value = IDLE_BATTERY
    streaming_vehicle.get_exterior.return_value = LOCKED
    entry = MockConfigEntry(
        domain="polestar",
        unique_id="LPSVSEDEEPL000001",
        version=2,
        data={"email": "u@example.com", "password": "p", "vin": "LPSVSEDEEPL000001"},
        options={CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL, CONF_ENABLE_STREAMS: True},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.polestar.PolestarApi", return_value=mock_api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def test_only_the_valuable_attrs_stream() -> None:
    """Every open stream costs ~720 calls a day, so the set stays small."""
    assert set(ALWAYS_ON_STREAMS) == {"battery", "exterior", "climate"}
    assert set(ACTIVE_ONLY_STREAMS) == {"location"}
    assert set(STREAM_METHODS) == set(ALWAYS_ON_STREAMS) | set(ACTIVE_ONLY_STREAMS)


async def test_active_car_streams_every_streamable_attr(
    streaming_setup, hass: HomeAssistant
) -> None:
    """The default fixture battery is charging, which makes the car active."""
    coordinator = next(iter(streaming_setup.runtime_data.coordinators.values()))
    assert coordinator.car_is_active
    assert _running(coordinator) == set(STREAM_METHODS)


async def test_idle_car_streams_only_the_always_on_attrs(
    idle_streaming_setup, hass: HomeAssistant
) -> None:
    coordinator = next(iter(idle_streaming_setup.runtime_data.coordinators.values()))
    assert not coordinator.car_is_active
    assert _running(coordinator) == set(ALWAYS_ON_STREAMS)


async def test_active_only_streams_follow_the_car_state(
    idle_streaming_setup, hass: HomeAssistant, streaming_vehicle
) -> None:
    """An unlock pushed over the exterior stream wakes location up; locking
    again puts it back to sleep."""
    coordinator = next(iter(idle_streaming_setup.runtime_data.coordinators.values()))

    await streaming_vehicle.stream_updates["exterior"].put(UNLOCKED)
    await hass.async_block_till_done()
    assert _running(coordinator) == set(STREAM_METHODS)
    location_task = coordinator._stream_tasks["location"]

    await streaming_vehicle.stream_updates["exterior"].put(LOCKED)
    await hass.async_block_till_done()
    assert set(coordinator._stream_tasks) == set(ALWAYS_ON_STREAMS)
    await asyncio.gather(location_task, return_exceptions=True)
    assert location_task.cancelled()


async def test_poll_does_not_restart_active_only_streams_while_idle(
    idle_streaming_setup, hass: HomeAssistant
) -> None:
    coordinator = next(iter(idle_streaming_setup.runtime_data.coordinators.values()))

    await coordinator.async_refresh()

    assert _running(coordinator) == set(ALWAYS_ON_STREAMS)


async def test_diagnostics_list_every_stream_with_its_policy(
    idle_streaming_setup, hass: HomeAssistant
) -> None:
    diag = await async_get_config_entry_diagnostics(hass, idle_streaming_setup)

    streams = diag["vehicles"][0]["streams"]
    assert set(streams) == set(STREAM_METHODS)
    assert streams["battery"] == {
        "policy": "always_on", "started": True, "running": True, "supported": True
    }
    assert streams["location"]["policy"] == "active_only"
    assert not streams["location"]["started"]


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


@pytest.mark.parametrize(
    ("stream_lifetime", "expected_wait"),
    [
        (0.0, STREAM_MIN_RESUBSCRIBE_INTERVAL),
        (STREAM_MIN_RESUBSCRIBE_INTERVAL / 3, STREAM_MIN_RESUBSCRIBE_INTERVAL * 2 / 3),
        (120.0, None),
    ],
)
async def test_resubscribe_after_clean_close_respects_the_floor(
    coordinator, battery, stream_lifetime, expected_wait
) -> None:
    """A server closing after every message must not become a tight loop."""
    clock = [1_000.0]
    subscribes = 0

    async def _stream():
        nonlocal subscribes
        subscribes += 1
        if subscribes > 3:
            raise asyncio.CancelledError
        clock[0] += stream_lifetime
        yield battery

    sleep = AsyncMock()
    with (
        patch("custom_components.polestar.coordinator.monotonic", side_effect=lambda: clock[0]),
        patch("custom_components.polestar.coordinator.asyncio.sleep", sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await coordinator._async_run_stream("battery", _stream)

    assert subscribes == 4
    if expected_wait is None:
        sleep.assert_not_called()
    else:
        assert [c.args[0] for c in sleep.await_args_list] == pytest.approx([expected_wait] * 3)
