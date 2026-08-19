"""Setup and teardown for the Polestar integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.polestar.const import CONF_DEMO, CONF_VIN, DOMAIN


async def test_setup_and_unload(hass: HomeAssistant, setup_integration) -> None:
    """The entry sets up, exposes runtime data, and unloads cleanly."""
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinators

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_coordinator_is_primed(setup_integration, coordinator) -> None:
    """The coordinator's first poll has already run by the time setup returns."""
    assert coordinator.last_update_success
    assert coordinator.data.battery is not None


async def test_coordinator_polls_on_a_real_interval(coordinator) -> None:
    """The single-coordinator design polls on its own timer, unlike the old hub."""
    assert coordinator.update_interval is not None


async def test_demo_mode_sets_up(hass: HomeAssistant) -> None:
    """Demo mode must survive coordinator changes too."""
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
