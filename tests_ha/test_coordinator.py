"""Coordinator behaviour: tier composition, error handling, merge rules."""

from __future__ import annotations

from datetime import timedelta

import pytest
from grpclib.const import Status as GrpcStatus
from grpclib.exceptions import GRPCError
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.polestar.const import (
    DEFAULT_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    TIER_MULTIPLIERS,
)
from custom_components.polestar.coordinator import (
    _FETCH_ATTRS,
    _UNSUPPORTED_REPROBE_CYCLES,
    STREAM_METHODS,
    TIER_ATTRS,
    TIER_FAST,
    TIER_ORDER,
    TIER_SLOW,
    PolestarVehicleData,
    tier_intervals,
)
from polestar_api.exceptions import AuthError
from polestar_api.models.odometer import OdometerStatus


# ---------------------------------------------------------------------------
# Static invariants
# ---------------------------------------------------------------------------


def test_every_fetchable_attr_is_polled_by_exactly_one_tier() -> None:
    """The odometer-staleness class of bug: an attr no tier owns never updates."""
    owned: list[str] = [attr for tier in TIER_ORDER for attr in TIER_ATTRS[tier]]

    assert sorted(owned) == sorted(set(owned)), "an attribute is polled by two tiers"
    assert set(owned) == set(_FETCH_ATTRS), (
        "every fetchable attribute must belong to exactly one poll tier"
    )


def test_tier_attrs_covers_every_snapshot_field() -> None:
    """PolestarVehicleData must not grow a field nothing ever fills."""
    snapshot_fields = set(vars(PolestarVehicleData()))
    assert snapshot_fields == set(_FETCH_ATTRS)


def test_streamable_attrs_are_also_polled() -> None:
    """Streams accelerate polling; they must never be an attribute's only source."""
    assert set(STREAM_METHODS).issubset(set(_FETCH_ATTRS))


def test_tier_order_is_slowest_last() -> None:
    multipliers = [TIER_MULTIPLIERS[tier] for tier in TIER_ORDER]
    assert multipliers == sorted(multipliers)


@pytest.mark.parametrize(
    ("base", "expected_fast"),
    [
        (DEFAULT_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        (1, MIN_UPDATE_INTERVAL),
        (99999, MAX_UPDATE_INTERVAL),
    ],
)
def test_tier_intervals_are_clamped_and_scaled(base: int, expected_fast: int) -> None:
    intervals = tier_intervals(base)
    assert intervals[TIER_FAST] == timedelta(seconds=expected_fast)
    for tier in TIER_ORDER:
        assert intervals[tier] == timedelta(seconds=expected_fast * TIER_MULTIPLIERS[tier])


# ---------------------------------------------------------------------------
# Error handling — mirrors volvo's _async_update_data contract
# ---------------------------------------------------------------------------


async def test_single_call_failure_does_not_kill_the_tier(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """One endpoint erroring must not discard the rest of the tier's results."""
    mock_vehicle.get_climate.side_effect = OSError("boom")
    previous_climate = coordinator.data.climate

    tier = coordinator.tiers[TIER_FAST]
    await tier.async_refresh()

    assert tier.last_update_success
    assert coordinator.data.battery is not None
    # The failed attribute keeps its last good value rather than being cleared.
    assert coordinator.data.climate is previous_climate


async def test_total_tier_failure_raises_update_failed(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """Every call in a tier failing marks that tier unsuccessful."""
    for attr in TIER_ATTRS[TIER_FAST]:
        getattr(mock_vehicle, _FETCH_ATTRS[attr]).side_effect = OSError("offline")

    tier = coordinator.tiers[TIER_FAST]
    await tier.async_refresh()
    assert not tier.last_update_success


async def test_entities_stay_available_while_any_tier_succeeds(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """A single dead tier must not black out every entity."""
    for attr in TIER_ATTRS[TIER_FAST]:
        getattr(mock_vehicle, _FETCH_ATTRS[attr]).side_effect = OSError("offline")

    await coordinator.tiers[TIER_FAST].async_refresh()

    assert not coordinator.tiers[TIER_FAST].last_update_success
    assert coordinator.last_update_success, "other tiers are healthy"


async def test_all_tiers_failing_marks_the_hub_unavailable(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    for method_name in _FETCH_ATTRS.values():
        getattr(mock_vehicle, method_name).side_effect = OSError("offline")

    for tier in coordinator.tiers.values():
        await tier.async_refresh()

    assert not coordinator.last_update_success


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """An AuthError must raise ConfigEntryAuthFailed, not be swallowed per-call."""
    mock_vehicle.get_battery.side_effect = AuthError("token rejected")

    await coordinator.tiers[TIER_FAST].async_refresh()
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
    tier = coordinator.tiers[TIER_SLOW]
    assert "health" in tier.attrs

    mock_vehicle.get_health.side_effect = GRPCError(GrpcStatus.UNIMPLEMENTED, "nope")
    await tier.async_refresh()

    assert coordinator.data.health is None
    assert "health" in coordinator._unsupported_fetches

    # Subsequent polls skip it entirely rather than re-erroring every cycle.
    mock_vehicle.get_health.reset_mock()
    await tier.async_refresh()
    mock_vehicle.get_health.assert_not_called()


async def test_unsupported_endpoint_is_reprobed_periodically(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """A backend that starts serving an endpoint again must be picked up."""
    tier = coordinator.tiers[TIER_SLOW]
    mock_vehicle.get_health.side_effect = GRPCError(GrpcStatus.UNIMPLEMENTED, "nope")
    await tier.async_refresh()
    assert "health" in coordinator._unsupported_fetches

    mock_vehicle.get_health.side_effect = None
    mock_vehicle.get_health.return_value = "healthy"

    called = False
    for _ in range(_UNSUPPORTED_REPROBE_CYCLES * 2):
        mock_vehicle.get_health.reset_mock()
        await tier.async_refresh()
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
    tier = coordinator.tiers["medium"]
    mock_vehicle.get_odometer.return_value = OdometerStatus(odometer_meters=2_000_000)
    await tier.async_refresh()
    assert coordinator.data.odometer.odometer_meters == 2_000_000

    mock_vehicle.get_odometer.return_value = OdometerStatus(odometer_meters=1_500_000)
    await tier.async_refresh()
    assert coordinator.data.odometer.odometer_meters == 2_000_000


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


async def test_fast_tier_keeps_polling_on_its_timer(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """Regression guard: a tier with no listener would silently stop polling."""
    tier = coordinator.tiers[TIER_FAST]
    mock_vehicle.get_battery.reset_mock()

    async_fire_time_changed(hass, dt_util.utcnow() + tier.update_interval * 2)
    await hass.async_block_till_done()

    assert mock_vehicle.get_battery.called


async def test_slow_tier_does_not_poll_on_the_fast_interval(
    hass: HomeAssistant, coordinator, mock_vehicle
) -> None:
    """The whole point of tiering: rarely-changing data is not re-fetched often."""
    fast_interval = coordinator.tiers[TIER_FAST].update_interval
    mock_vehicle.get_weather.reset_mock()

    async_fire_time_changed(hass, dt_util.utcnow() + fast_interval + timedelta(seconds=1))
    await hass.async_block_till_done()

    mock_vehicle.get_weather.assert_not_called()
