"""Setup, teardown and tier wiring for the Polestar integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.polestar.const import CONF_DEMO, CONF_VIN, DOMAIN
from custom_components.polestar.coordinator import TIER_ORDER


async def test_setup_and_unload(hass: HomeAssistant, setup_integration) -> None:
    """The entry sets up, exposes runtime data, and unloads cleanly."""
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinators

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_all_tiers_created_and_primed(setup_integration, coordinator) -> None:
    """Every tier exists, is primed, and holds a distinct interval."""
    assert set(coordinator.tiers) == set(TIER_ORDER)
    assert setup_integration.runtime_data.tiers

    intervals = [
        coordinator.tiers[tier].update_interval.total_seconds() for tier in TIER_ORDER
    ]
    assert intervals == sorted(intervals)
    assert len(set(intervals)) == len(TIER_ORDER)

    for tier in coordinator.tiers.values():
        assert tier.last_update_success


async def test_tiers_have_listeners_so_their_timers_run(coordinator) -> None:
    """A DataUpdateCoordinator only polls while it has a listener.

    Entities subscribe to the hub, not the tiers, so the hub must subscribe
    itself to each tier — without this the tiers would never poll again after
    the first refresh.
    """
    for tier in coordinator.tiers.values():
        assert tier._listeners, f"{tier.tier} tier has no listener, its timer is idle"


async def test_hub_has_no_interval_of_its_own(coordinator) -> None:
    """The hub never polls on a timer; the tiers do."""
    assert coordinator.update_interval is None


async def test_demo_mode_sets_up(hass: HomeAssistant) -> None:
    """Demo mode must survive the coordinator rewrite too."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="DEMO000000000001",
        version=2,
        data={CONF_VIN: "DEMO000000000001", CONF_DEMO: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    demo_coordinator = next(iter(entry.runtime_data.coordinators.values()))
    assert demo_coordinator.data.battery is not None
    assert len(demo_coordinator.tiers) == 4
