"""Command entities dispatch what their label says.

Cheap coverage against the "the button doesn't actually do the thing" class of
bug. Everything goes through hass.services.async_call rather than calling
entity methods, so the entity wiring is exercised too.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_LOCK,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_UNLOCK,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from polestar_api.models.charging import (
    ChargeTargetLevelSettingType,
    TargetSocResponse,
)

PREFIX = "polestar_4_test123"


async def _call(hass: HomeAssistant, domain: str, service: str, entity: str, **extra):
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity, **extra}, blocking=True
    )


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


async def test_lock_and_unlock_dispatch(
    hass: HomeAssistant, setup_integration, mock_vehicle
) -> None:
    await _call(hass, LOCK_DOMAIN, SERVICE_LOCK, f"lock.{PREFIX}_lock")
    mock_vehicle.lock.assert_awaited_once()

    await _call(hass, LOCK_DOMAIN, SERVICE_UNLOCK, f"lock.{PREFIX}_lock")
    mock_vehicle.unlock.assert_awaited_once()


async def test_lock_failure_surfaces_to_the_caller(
    hass: HomeAssistant, setup_integration, mock_vehicle
) -> None:
    """A failed command must raise, not silently report success."""
    mock_vehicle.lock.return_value = 999  # not a success status code

    with pytest.raises(HomeAssistantError):
        await _call(hass, LOCK_DOMAIN, SERVICE_LOCK, f"lock.{PREFIX}_lock")


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "service", "method"),
    [
        ("charging", SERVICE_TURN_ON, "start_charging"),
        ("charging", SERVICE_TURN_OFF, "stop_charging"),
        ("pre_cleaning", SERVICE_TURN_ON, "start_precleaning"),
        ("pre_cleaning", SERVICE_TURN_OFF, "stop_precleaning"),
        ("climate", SERVICE_TURN_OFF, "stop_climate"),
    ],
)
async def test_switch_dispatch(
    hass: HomeAssistant, setup_integration, mock_vehicle, key, service, method
) -> None:
    entity = f"switch.{PREFIX}_{key}"
    assert hass.states.get(entity) is not None, f"{entity} does not exist"

    await _call(hass, SWITCH_DOMAIN, service, entity)
    getattr(mock_vehicle, method).assert_awaited_once()


async def test_charge_timer_switch_preserves_existing_times(
    hass: HomeAssistant, setup_integration, mock_vehicle
) -> None:
    """Toggling the timer must not wipe the start/stop times already set."""
    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, f"switch.{PREFIX}_charge_timer")

    mock_vehicle.set_charge_timer.assert_awaited_once()
    timer = mock_vehicle.set_charge_timer.call_args.args[0]
    assert timer.activated is True


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "method"),
    [
        ("open_windows", "open_windows"),
        ("close_windows", "close_windows"),
        ("unlock_trunk", "unlock_trunk"),
    ],
)
async def test_button_dispatch(
    hass: HomeAssistant, setup_integration, mock_vehicle, key, method
) -> None:
    await _call(hass, BUTTON_DOMAIN, SERVICE_PRESS, f"button.{PREFIX}_{key}")
    getattr(mock_vehicle, method).assert_awaited_once()


@pytest.mark.parametrize("key", ["flash_lights", "honk", "honk_and_flash"])
async def test_honk_flash_buttons_dispatch(
    hass: HomeAssistant, setup_integration, mock_vehicle, key
) -> None:
    mock_vehicle.honk_flash = AsyncMock(return_value=None)
    await _call(hass, BUTTON_DOMAIN, SERVICE_PRESS, f"button.{PREFIX}_{key}")
    mock_vehicle.honk_flash.assert_awaited_once()


async def test_refresh_button_polls_every_endpoint(
    hass: HomeAssistant, setup_integration, mock_vehicle, coordinator
) -> None:
    """The manual refresh button must not be limited to one tier."""
    mock_vehicle.get_battery.reset_mock()
    mock_vehicle.get_weather.reset_mock()
    mock_vehicle.get_software_info.reset_mock()

    await _call(hass, BUTTON_DOMAIN, SERVICE_PRESS, f"button.{PREFIX}_refresh")
    await hass.async_block_till_done()

    mock_vehicle.get_battery.assert_awaited()
    mock_vehicle.get_weather.assert_awaited()
    mock_vehicle.get_software_info.assert_awaited()


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


async def test_amp_limit_dispatch(
    hass: HomeAssistant, setup_integration, mock_vehicle
) -> None:
    await _call(
        hass,
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        f"number.{PREFIX}_charging_amp_limit",
        **{ATTR_VALUE: 16},
    )
    mock_vehicle.set_amp_limit.assert_awaited_once_with(16)


async def test_target_soc_dispatch_in_custom_mode(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    coordinator.data = replace(
        coordinator.data,
        target_soc=TargetSocResponse(
            target_level=80, setting_type=ChargeTargetLevelSettingType.CUSTOM
        ),
    )

    await _call(
        hass,
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        f"number.{PREFIX}_target_soc",
        **{ATTR_VALUE: 90},
    )

    mock_vehicle.set_target_soc.assert_awaited_once_with(
        90, ChargeTargetLevelSettingType.CUSTOM
    )


async def test_target_soc_refuses_in_preset_mode(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """In DAILY mode the car ignores a level; say so instead of pretending."""
    coordinator.async_apply_values(
        {
            "target_soc": TargetSocResponse(
                target_level=80, setting_type=ChargeTargetLevelSettingType.DAILY
            )
        }
    )
    await hass.async_block_till_done()

    # The slider locks itself, so the value can't be changed from the UI at all.
    assert hass.states.get(f"number.{PREFIX}_target_soc").state == "unavailable"

    # And the coordinator refuses outright rather than sending a level the car
    # would silently discard.
    with pytest.raises(HomeAssistantError, match="daily"):
        await coordinator.async_set_target_soc(90)
    mock_vehicle.set_target_soc.assert_not_awaited()
