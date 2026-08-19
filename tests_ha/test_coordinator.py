"""Coordinator behaviour: poll composition, error handling, merge rules."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from grpclib.const import Status as GrpcStatus
from grpclib.exceptions import GRPCError
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.polestar.const import (
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)
from custom_components.polestar.coordinator import (
    _FETCH_ATTRS,
    _UNSUPPORTED_REPROBE_CYCLES,
    STREAM_METHODS,
    PolestarVehicleData,
)
from polestar_api.exceptions import AuthError
from polestar_api.models.odometer import OdometerStatus


# ---------------------------------------------------------------------------
# Static invariants
# ---------------------------------------------------------------------------


def test_snapshot_fields_cover_every_fetchable_attr() -> None:
    """PolestarVehicleData must not grow a field nothing ever fills."""
    snapshot_fields = set(vars(PolestarVehicleData()))
    assert snapshot_fields == set(_FETCH_ATTRS)


def test_streamable_attrs_are_also_polled() -> None:
    """Streams accelerate polling; they must never be an attribute's only source."""
    assert set(STREAM_METHODS).issubset(set(_FETCH_ATTRS))


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (DEFAULT_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        (1, MIN_UPDATE_INTERVAL),
        (99999, MAX_UPDATE_INTERVAL),
    ],
)
async def test_update_interval_is_clamped(
    hass: HomeAssistant, mock_config_entry, mock_api, configured: int, expected: int
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_UPDATE_INTERVAL: configured}
    )
    with patch("custom_components.polestar.PolestarApi", return_value=mock_api):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    coordinator = next(iter(mock_config_entry.runtime_data.coordinators.values()))
    assert coordinator.update_interval == timedelta(seconds=expected)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_single_call_failure_does_not_kill_the_poll(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """One endpoint erroring must not discard the rest of the poll's results."""
    mock_vehicle.get_climate.side_effect = OSError("boom")
    previous_climate = coordinator.data.climate

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.battery is not None
    # The failed attribute keeps its last good value rather than being cleared.
    assert coordinator.data.climate is previous_climate


async def test_total_failure_raises_update_failed(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """Every call failing marks the coordinator unsuccessful."""
    for method_name in _FETCH_ATTRS.values():
        getattr(mock_vehicle, method_name).side_effect = OSError("offline")

    await coordinator.async_refresh()
    assert not coordinator.last_update_success


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """An AuthError must raise ConfigEntryAuthFailed, not be swallowed per-call."""
    mock_vehicle.get_battery.side_effect = AuthError("token rejected")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


# ---------------------------------------------------------------------------
# UNIMPLEMENTED handling
# ---------------------------------------------------------------------------


async def test_unimplemented_clears_the_attribute_and_is_then_skipped(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """A withdrawn endpoint must clear, not pin its sensors to a stale snapshot."""
    mock_vehicle.get_health.side_effect = GRPCError(GrpcStatus.UNIMPLEMENTED, "nope")
    await coordinator.async_refresh()

    assert coordinator.data.health is None
    assert "health" in coordinator._unsupported_fetches

    # Subsequent polls skip it entirely rather than re-erroring every cycle.
    mock_vehicle.get_health.reset_mock()
    await coordinator.async_refresh()
    mock_vehicle.get_health.assert_not_called()


async def test_unsupported_endpoint_is_reprobed_periodically(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """A backend that starts serving an endpoint again must be picked up."""
    mock_vehicle.get_health.side_effect = GRPCError(GrpcStatus.UNIMPLEMENTED, "nope")
    await coordinator.async_refresh()
    assert "health" in coordinator._unsupported_fetches

    mock_vehicle.get_health.side_effect = None
    mock_vehicle.get_health.return_value = "healthy"

    called = False
    for _ in range(_UNSUPPORTED_REPROBE_CYCLES * 2):
        mock_vehicle.get_health.reset_mock()
        await coordinator.async_refresh()
        if mock_vehicle.get_health.called:
            called = True
            break

    assert called, "an unsupported endpoint was never re-probed"
    assert coordinator.data.health == "healthy"
    assert "health" not in coordinator._unsupported_fetches


# ---------------------------------------------------------------------------
# Merge rules
# ---------------------------------------------------------------------------


async def test_odometer_never_goes_backwards(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """odometer_km is TOTAL_INCREASING; a stale snapshot would read as a reset."""
    mock_vehicle.get_odometer.return_value = OdometerStatus(odometer_meters=2_000_000)
    await coordinator.async_refresh()
    assert coordinator.data.odometer.odometer_meters == 2_000_000

    mock_vehicle.get_odometer.return_value = OdometerStatus(odometer_meters=1_500_000)
    await coordinator.async_refresh()
    assert coordinator.data.odometer.odometer_meters == 2_000_000


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


async def test_coordinator_keeps_polling_on_its_timer(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    mock_vehicle.get_battery.reset_mock()

    async_fire_time_changed(hass, dt_util.utcnow() + coordinator.update_interval * 2)
    await hass.async_block_till_done()

    assert mock_vehicle.get_battery.called
