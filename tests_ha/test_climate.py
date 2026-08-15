"""Climate command regressions.

These target bugs that reached the owner's production Home Assistant:

* turning the climate switch on sent ``temperature=0.0`` whenever the user had
  never moved the target-temperature slider, because HA cached its own default
  of 0.0 instead of reading what the car reported;
* moving the slider while climatisation was already running only updated that
  cache and never reached the car.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant

from custom_components.polestar.const import DEFAULT_CLIMATE_TEMPERATURE

CLIMATE_SWITCH = "switch.polestar_4_test123_climate"
TEMPERATURE_NUMBER = "number.polestar_4_test123_climate_target_temperature"


async def _turn_climate_on(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: CLIMATE_SWITCH},
        blocking=True,
    )


async def _set_temperature(hass: HomeAssistant, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: TEMPERATURE_NUMBER, ATTR_VALUE: value},
        blocking=True,
    )


# ---------------------------------------------------------------------------
# Bug 1 — climate start sent 0.0 °C
# ---------------------------------------------------------------------------


async def test_turn_on_uses_the_cars_reported_target_temperature(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """Never send 0.0 when the car has reported a real target."""
    await _turn_climate_on(hass)

    mock_vehicle.start_climate.assert_called_once()
    sent = mock_vehicle.start_climate.call_args.kwargs["temperature"]
    assert sent == 22.5
    assert sent != 0.0


async def test_turn_on_falls_back_to_a_valid_default(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """Even with no target from the car, the command must be valid."""
    coordinator.data = replace(coordinator.data, climate=None)

    await _turn_climate_on(hass)

    sent = mock_vehicle.start_climate.call_args.kwargs["temperature"]
    assert sent == DEFAULT_CLIMATE_TEMPERATURE
    assert sent > 0.0


@pytest.mark.parametrize("reported", [None, 0.0])
async def test_turn_on_never_sends_zero(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle, reported
) -> None:
    """The regression, stated directly: a start command is never 0.0 °C."""
    coordinator.data = replace(
        coordinator.data,
        climate=replace(coordinator.data.climate, target_temperature_celsius=reported),
    )

    await _turn_climate_on(hass)

    assert mock_vehicle.start_climate.call_args.kwargs["temperature"] > 0.0


async def test_slider_and_command_read_the_same_value(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """What the number entity displays is exactly what a start command sends.

    This is the structural fix: one property feeds both, so they cannot drift.
    """
    displayed = float(hass.states.get(TEMPERATURE_NUMBER).state)

    await _turn_climate_on(hass)

    assert mock_vehicle.start_climate.call_args.kwargs["temperature"] == displayed


async def test_number_entity_shows_the_cars_value_without_user_input(
    hass: HomeAssistant, setup_integration
) -> None:
    """No stale 0.0 in the UI before the user has touched anything."""
    assert hass.states.get(TEMPERATURE_NUMBER).state == "22.5"


# ---------------------------------------------------------------------------
# Bug 2 — slider did not push while climate was running
# ---------------------------------------------------------------------------


async def test_setting_temperature_while_climate_off_only_caches(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """With climatisation idle there is nothing to restart — just remember it."""
    await _set_temperature(hass, 19.0)

    mock_vehicle.start_climate.assert_not_called()
    assert coordinator.climate_target_temperature == 19.0


async def test_setting_temperature_while_climate_on_pushes_to_the_car(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle, climate_on
) -> None:
    """With climatisation running the new temperature must reach the car."""
    coordinator.data = replace(coordinator.data, climate=climate_on)
    assert coordinator.climate_is_active

    await _set_temperature(hass, 19.0)

    mock_vehicle.start_climate.assert_called_once()
    assert mock_vehicle.start_climate.call_args.kwargs["temperature"] == 19.0


async def test_user_override_wins_over_the_cars_value(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """A value the user chose is not overwritten by the next poll."""
    await _set_temperature(hass, 18.0)
    assert coordinator.climate_target_temperature == 18.0

    await coordinator.tiers["fast"].async_refresh()

    assert coordinator.climate_target_temperature == 18.0
    await _turn_climate_on(hass)
    assert mock_vehicle.start_climate.call_args.kwargs["temperature"] == 18.0


async def test_seat_heating_preferences_still_flow_through(
    hass: HomeAssistant, setup_integration, coordinator, mock_vehicle
) -> None:
    """Removing target_temperature from the prefs must not break the seat prefs."""
    from polestar_api.models.climatization import HeatingIntensity

    coordinator.climate_preferences.front_left_seat = HeatingIntensity.LEVEL3

    await _turn_climate_on(hass)

    kwargs = mock_vehicle.start_climate.call_args.kwargs
    assert kwargs["front_left_seat"] is HeatingIntensity.LEVEL3
    assert not hasattr(coordinator.climate_preferences, "target_temperature")
