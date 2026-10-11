"""Coordinator behaviour: poll composition, error handling, merge rules."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from grpclib.const import Status as GrpcStatus
from grpclib.exceptions import GRPCError
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.polestar.const import (
    ACTIVE_UPDATE_INTERVAL_POLLING,
    ACTIVE_UPDATE_INTERVAL_STREAMING,
    COMMAND_ACTIVE_WINDOW,
    CONF_ENABLE_STREAMS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)
from custom_components.polestar.coordinator import (
    _FETCH_ATTRS,
    _INVALID_ARGUMENT_UNSUPPORTED_AFTER,
    _UNSUPPORTED_REPROBE_INTERVAL,
    STREAM_METHODS,
    PolestarVehicleData,
)
from custom_components.polestar.diagnostics import async_get_config_entry_diagnostics
from polestar_api.exceptions import AuthCooldownError, AuthError
from polestar_api.models.availability import Availability, UsageMode
from polestar_api.models.battery import Battery, ChargingStatus
from polestar_api.models.exterior import (
    CentralLockStatus,
    DoorsStatus,
    DoorStatus,
    ExteriorStatus,
    LockStatus,
    OpenStatus,
    TailgateStatus,
    WindowsStatus,
    WindowStatus,
)
from polestar_api.models.odometer import OdometerStatus

IDLE_BATTERY = Battery(charge_level=62, charging_status=ChargingStatus.IDLE, range_km=310)
LOCKED = ExteriorStatus(central_lock=CentralLockStatus(lock_status=LockStatus.LOCKED))


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
    hass: HomeAssistant,
    mock_config_entry,
    mock_api,
    mock_vehicle,
    configured: int,
    expected: int,
) -> None:
    """The option is the idle interval, so assert it on an idle car."""
    mock_vehicle.get_battery.return_value = IDLE_BATTERY
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


async def test_reauth_cooldown_is_transient_not_a_reauth(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """A login that failed on a network blip must not lock the user out.

    During the re-login cooldown calls fail fast; that has to read as a
    transient fetch failure that heals itself, not as rejected credentials.
    """
    for method in _FETCH_ATTRS.values():
        getattr(mock_vehicle, method).side_effect = AuthCooldownError("cooldown")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    flows = hass.config_entries.flow.async_progress()
    assert not any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


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


async def test_unsupported_endpoint_is_reprobed_once_a_day(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """A backend that starts serving an endpoint again must be picked up,
    without hammering an endpoint the car does not support on every poll."""
    clock = [1_000_000.0]
    with patch(
        "custom_components.polestar.coordinator.monotonic", side_effect=lambda: clock[0]
    ):
        coordinator._last_unsupported_probe = None
        mock_vehicle.get_health.side_effect = GRPCError(GrpcStatus.UNIMPLEMENTED, "nope")
        await coordinator.async_refresh()
        assert "health" in coordinator._unsupported_fetches

        mock_vehicle.get_health.side_effect = None
        mock_vehicle.get_health.return_value = "healthy"

        for _ in range(20):
            clock[0] += 300
            mock_vehicle.get_health.reset_mock()
            await coordinator.async_refresh()
            mock_vehicle.get_health.assert_not_called()

        clock[0] += _UNSUPPORTED_REPROBE_INTERVAL
        await coordinator.async_refresh()

    mock_vehicle.get_health.assert_called_once()
    assert coordinator.data.health == "healthy"
    assert "health" not in coordinator._unsupported_fetches


async def test_repeated_invalid_argument_marks_the_endpoint_unsupported(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """The P4 rejects charge-location reads with INVALID_ARGUMENT every time;
    after a few in a row they are skipped like UNIMPLEMENTED, not retried and
    logged on every poll."""
    mock_vehicle.get_charge_locations.side_effect = GRPCError(
        GrpcStatus.INVALID_ARGUMENT, "Validation failed"
    )
    for _ in range(_INVALID_ARGUMENT_UNSUPPORTED_AFTER - 1):
        await coordinator.async_refresh()
        assert "charge_locations" not in coordinator._unsupported_fetches

    await coordinator.async_refresh()
    assert "charge_locations" in coordinator._unsupported_fetches
    assert coordinator.data.charge_locations == []

    mock_vehicle.get_charge_locations.reset_mock()
    await coordinator.async_refresh()
    mock_vehicle.get_charge_locations.assert_not_called()


async def test_a_success_resets_the_invalid_argument_count(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """Only consecutive INVALID_ARGUMENTs count; one transient error never
    takes an endpoint off the poll."""
    error = GRPCError(GrpcStatus.INVALID_ARGUMENT, "Validation failed")
    for _ in range(3):
        mock_vehicle.get_charge_locations.side_effect = error
        for _ in range(_INVALID_ARGUMENT_UNSUPPORTED_AFTER - 1):
            await coordinator.async_refresh()
        mock_vehicle.get_charge_locations.side_effect = None
        await coordinator.async_refresh()

    assert "charge_locations" not in coordinator._unsupported_fetches


async def test_post_command_refresh_skips_unsupported_endpoints(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """A command's follow-up refreshes must not re-call an UNIMPLEMENTED endpoint."""
    mock_vehicle.get_amp_limit.side_effect = GRPCError(GrpcStatus.UNIMPLEMENTED, "nope")
    await coordinator.async_refresh()
    assert "amp_limit" in coordinator._unsupported_fetches

    mock_vehicle.get_amp_limit.reset_mock()
    await coordinator.async_request_attrs_refresh("amp_limit", "battery")

    mock_vehicle.get_amp_limit.assert_not_called()
    mock_vehicle.get_battery.assert_called()


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


# ---------------------------------------------------------------------------
# Active / idle
# ---------------------------------------------------------------------------


async def _setup(hass: HomeAssistant, entry, api, **options):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options=options)
    with patch("custom_components.polestar.PolestarApi", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return next(iter(entry.runtime_data.coordinators.values()))


@pytest.fixture
async def idle_coordinator(hass: HomeAssistant, mock_config_entry, mock_api, mock_vehicle):
    """A coordinator for a parked, locked car that is not charging."""
    mock_vehicle.get_battery.return_value = IDLE_BATTERY
    mock_vehicle.get_exterior.return_value = LOCKED
    return await _setup(
        hass, mock_config_entry, mock_api, **{CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL}
    )


def _door(status: OpenStatus) -> ExteriorStatus:
    return ExteriorStatus(
        central_lock=CentralLockStatus(lock_status=LockStatus.LOCKED),
        doors=DoorsStatus(rear_left=DoorStatus(open_status=status)),
    )


@pytest.mark.parametrize(
    ("values", "active"),
    [
        ({}, False),
        ({"battery": Battery(charging_status=ChargingStatus.CHARGING)}, True),
        ({"battery": Battery(charging_status=ChargingStatus.SMART_CHARGING_PAUSED)}, False),
        ({"battery": Battery(charging_status=ChargingStatus.DONE)}, False),
        ({"climate": "climate_on"}, True),
        ({"exterior": ExteriorStatus(
            central_lock=CentralLockStatus(lock_status=LockStatus.UNLOCKED)
        )}, True),
        ({"exterior": ExteriorStatus()}, False),
        ({"exterior": _door(OpenStatus.OPEN)}, True),
        ({"exterior": _door(OpenStatus.AJAR)}, True),
        ({"exterior": _door(OpenStatus.CLOSED)}, False),
        ({"exterior": ExteriorStatus(
            windows=WindowsStatus(front_right=WindowStatus(open_status=OpenStatus.AJAR))
        )}, True),
        ({"exterior": ExteriorStatus(
            tailgate=TailgateStatus(status=DoorStatus(open_status=OpenStatus.OPEN))
        )}, True),
        ({"availability": Availability(usage_mode=UsageMode.DRIVING)}, True),
        ({"availability": Availability(usage_mode=UsageMode.ACTIVE)}, True),
        ({"availability": Availability(usage_mode=UsageMode.CONVENIENCE)}, True),
        ({"availability": Availability(usage_mode=UsageMode.ENGINE_ON)}, True),
        ({"availability": Availability(usage_mode=UsageMode.INACTIVE)}, False),
        ({"availability": Availability(usage_mode=UsageMode.ENGINE_OFF)}, False),
    ],
)
async def test_activity_rules(idle_coordinator, request, values, active) -> None:
    if values.get("climate") == "climate_on":
        values = {"climate": request.getfixturevalue("climate_on")}
    assert idle_coordinator.mode == "idle"

    idle_coordinator.async_apply_values(values)

    assert idle_coordinator.car_is_active is active
    assert idle_coordinator.mode == ("active" if active else "idle")


async def test_interval_follows_the_mode(idle_coordinator, battery) -> None:
    assert idle_coordinator.update_interval == timedelta(seconds=DEFAULT_UPDATE_INTERVAL)

    idle_coordinator.async_apply_values({"battery": battery})
    assert idle_coordinator.update_interval == timedelta(
        seconds=ACTIVE_UPDATE_INTERVAL_STREAMING
    )

    idle_coordinator.async_apply_values({"battery": IDLE_BATTERY})
    assert idle_coordinator.update_interval == timedelta(seconds=DEFAULT_UPDATE_INTERVAL)


@pytest.mark.parametrize(
    ("options", "expected_active"),
    [
        ({CONF_ENABLE_STREAMS: True}, ACTIVE_UPDATE_INTERVAL_STREAMING),
        ({CONF_ENABLE_STREAMS: False}, ACTIVE_UPDATE_INTERVAL_POLLING),
        # Never slower when active than the user asked for when idle.
        ({CONF_ENABLE_STREAMS: True, CONF_UPDATE_INTERVAL: 300}, 300),
    ],
)
async def test_active_interval(
    hass: HomeAssistant, mock_config_entry, mock_api, mock_vehicle, options, expected_active
) -> None:
    mock_vehicle.get_battery.return_value = Battery(charging_status=ChargingStatus.CHARGING)
    coordinator = await _setup(hass, mock_config_entry, mock_api, **options)

    assert coordinator.mode == "active"
    assert coordinator.update_interval == timedelta(seconds=expected_active)


async def test_a_command_makes_the_car_active_for_a_while(idle_coordinator) -> None:
    clock = [5_000.0]
    with patch(
        "custom_components.polestar.coordinator.monotonic", side_effect=lambda: clock[0]
    ):
        await idle_coordinator.async_run_command(AsyncMock(return_value=None))
        assert idle_coordinator.mode == "active"

        clock[0] += COMMAND_ACTIVE_WINDOW - 1
        idle_coordinator.async_apply_values({"battery": IDLE_BATTERY})
        assert idle_coordinator.mode == "active"

        clock[0] += 2
        idle_coordinator.async_apply_values({"battery": IDLE_BATTERY})
        assert idle_coordinator.mode == "idle"


async def test_waking_up_pulls_the_next_poll_in(
    hass: HomeAssistant, idle_coordinator, mock_vehicle, battery
) -> None:
    """An unlock/charge seen between polls must not wait out the idle timer."""
    mock_vehicle.get_battery.reset_mock()
    idle_coordinator.async_apply_values({"battery": battery})

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=ACTIVE_UPDATE_INTERVAL_STREAMING + 1)
    )
    await hass.async_block_till_done()

    mock_vehicle.get_battery.assert_called()


async def test_idle_car_waits_for_the_idle_interval(
    hass: HomeAssistant, idle_coordinator, mock_vehicle
) -> None:
    mock_vehicle.get_battery.reset_mock()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=ACTIVE_UPDATE_INTERVAL_STREAMING + 1)
    )
    await hass.async_block_till_done()
    mock_vehicle.get_battery.assert_not_called()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_UPDATE_INTERVAL + 1)
    )
    await hass.async_block_till_done()
    mock_vehicle.get_battery.assert_called()


async def test_diagnostics_report_mode_and_interval(
    hass: HomeAssistant, mock_config_entry, idle_coordinator
) -> None:
    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    poll = diag["vehicles"][0]["poll"]
    assert poll["mode"] == "idle"
    assert poll["interval_seconds"] == DEFAULT_UPDATE_INTERVAL
    assert poll["idle_interval_seconds"] == DEFAULT_UPDATE_INTERVAL
    assert poll["active_interval_seconds"] == ACTIVE_UPDATE_INTERVAL_STREAMING
