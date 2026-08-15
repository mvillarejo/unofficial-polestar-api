"""Entity identity is a compatibility surface, not an implementation detail.

Dashboards, scripts, automations and long-term statistics all key off
``entity_id``, which HA derives from ``unique_id`` the first time an entity is
registered. The coordinator rewrite deliberately kept the hub as the object
entities subscribe to so that ``f"{vin}_{key}"`` unique_ids stayed byte
identical; these tests fail loudly if that ever stops being true.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import TEST_VIN

# A sample spanning every platform. Not exhaustive by design — the whole-set
# assertions below cover the rest.
EXPECTED_UNIQUE_IDS = {
    f"{TEST_VIN}_battery_level",
    f"{TEST_VIN}_climate",
    f"{TEST_VIN}_climate_target_temperature",
    f"{TEST_VIN}_target_soc",
    f"{TEST_VIN}_amp_limit",
    f"{TEST_VIN}_location",
    f"{TEST_VIN}_parked_location",
    f"{TEST_VIN}_charge_timer",
    f"{TEST_VIN}_charge_timer_start",
    f"{TEST_VIN}_charge_timer_stop",
    f"{TEST_VIN}_software_update",
}


def _entries(hass: HomeAssistant, entry_id: str) -> list[er.RegistryEntry]:
    registry = er.async_get(hass)
    return er.async_entries_for_config_entry(registry, entry_id)


async def test_expected_unique_ids_are_present(
    hass: HomeAssistant, setup_integration
) -> None:
    """Known unique_ids from the pre-rewrite integration still exist."""
    present = {e.unique_id for e in _entries(hass, setup_integration.entry_id)}
    missing = EXPECTED_UNIQUE_IDS - present
    assert not missing, f"unique_ids disappeared in the rewrite: {sorted(missing)}"


async def test_every_unique_id_is_vin_prefixed(
    hass: HomeAssistant, setup_integration
) -> None:
    """The f"{vin}_{key}" convention must hold for every entity."""
    for entry in _entries(hass, setup_integration.entry_id):
        assert entry.unique_id.startswith(f"{TEST_VIN}_"), entry.entity_id


async def test_unique_ids_are_unique_within_each_platform(
    hass: HomeAssistant, setup_integration
) -> None:
    """HA scopes unique_id per platform domain, so only collisions there matter.

    Several keys deliberately appear twice across domains — target_soc is both
    a read-only sensor and a settable number, for instance.
    """
    entries = _entries(hass, setup_integration.entry_id)
    keys = [(e.domain, e.unique_id) for e in entries]
    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"colliding unique_ids: {sorted(duplicates)}"


async def test_all_entities_share_one_device(
    hass: HomeAssistant, setup_integration
) -> None:
    """Splitting the coordinator must not split the device."""
    device_ids = {e.device_id for e in _entries(hass, setup_integration.entry_id)}
    assert len(device_ids) == 1


async def test_every_platform_produced_entities(
    hass: HomeAssistant, setup_integration
) -> None:
    """A platform silently failing to load would be invisible otherwise."""
    from custom_components.polestar.const import PLATFORMS

    domains = {e.domain for e in _entries(hass, setup_integration.entry_id)}
    assert domains == set(PLATFORMS)


async def test_entities_are_available_after_setup(
    hass: HomeAssistant, setup_integration
) -> None:
    """The hub reports success once its tiers have primed."""
    state = hass.states.get("sensor.polestar_4_test123_battery_level")
    assert state is not None
    assert state.state == "62"
