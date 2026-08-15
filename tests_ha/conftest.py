"""Fixtures for the Home Assistant integration tests.

Mocking happens at the ``polestar_api.vehicle.Vehicle`` facade, not at the gRPC
wire, mirroring how HA core's ``tesla_fleet`` tests patch their API client's
methods. The wire format is already covered by the library suite in ``tests/``;
these tests exist to cover the HA layer's own logic.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.polestar.const import (
    CONF_UPDATE_INTERVAL,
    CONF_VIN,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from custom_components.polestar.coordinator import _FETCH_ATTRS
from polestar_api.models.battery import Battery, ChargingStatus
from polestar_api.models.climate import ClimatizationInfo, ClimatizationRunningStatus
from polestar_api.models.exterior import ExteriorStatus
from polestar_api.models.odometer import OdometerStatus

TEST_VIN = "LPSVSEDEEPL000001"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make custom_components/polestar loadable in every test."""
    return


@pytest.fixture
def battery() -> Battery:
    """A charging battery snapshot."""
    return Battery(charge_level=62, charging_status=ChargingStatus.CHARGING, range_km=310)


@pytest.fixture
def climate_off() -> ClimatizationInfo:
    """Climatisation idle, with a real target temperature reported by the car."""
    return ClimatizationInfo(
        running_status=ClimatizationRunningStatus.IDLE,
        target_temperature_celsius=22.5,
    )


@pytest.fixture
def climate_on() -> ClimatizationInfo:
    """Climatisation running."""
    return ClimatizationInfo(
        running_status=ClimatizationRunningStatus.ACTIVE,
        target_temperature_celsius=22.5,
    )


@pytest.fixture
def mock_vehicle(battery, climate_off) -> MagicMock:
    """A Vehicle facade whose every get_*/command method is mocked."""
    vehicle = MagicMock()
    vehicle.vin = TEST_VIN
    vehicle.model_name = "Polestar 4"
    vehicle.model_year = 2025
    vehicle.registration_no = "TEST123"

    defaults: dict[str, Any] = {
        "battery": battery,
        "climate": climate_off,
        "exterior": ExteriorStatus(),
        "odometer": OdometerStatus(odometer_meters=1_000_000),
        "charge_locations": [],
        "current_charge_location": {},
        "climate_timers": [],
    }
    for attr, method_name in _FETCH_ATTRS.items():
        setattr(vehicle, method_name, AsyncMock(return_value=defaults.get(attr)))

    for command in (
        "start_climate",
        "stop_climate",
        "start_charging",
        "stop_charging",
        "start_precleaning",
        "stop_precleaning",
        "open_windows",
        "close_windows",
        "unlock_trunk",
        "lock",
        "unlock",
        "set_target_soc",
        "set_amp_limit",
        "set_charge_timer",
    ):
        setattr(vehicle, command, AsyncMock(return_value=None))

    # No stream_* attributes: streams are opt-in and off by default, and
    # getattr(...) returning a MagicMock would otherwise start fake tasks.
    for method_name in list(vars(type(vehicle))):
        if method_name.startswith("stream_"):  # pragma: no cover - defensive
            delattr(vehicle, method_name)
    return vehicle


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A configured Polestar config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Polestar ({TEST_VIN})",
        unique_id=TEST_VIN,
        version=2,
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_VIN: TEST_VIN,
        },
        options={CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL},
    )


@pytest.fixture
def mock_api(mock_vehicle) -> MagicMock:
    """A PolestarApi whose vehicle list contains the mocked vehicle."""
    api = MagicMock()
    api.async_init = AsyncMock(return_value=None)
    api.get_vehicles = AsyncMock(return_value=[mock_vehicle])
    api.close = AsyncMock(return_value=None)
    return api


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: MagicMock
) -> MockConfigEntry:
    """Set the integration up against the mocked API."""
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.polestar.PolestarApi", return_value=mock_api):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    return mock_config_entry


@pytest.fixture
def coordinator(setup_integration, hass: HomeAssistant):
    """The hub coordinator of the set-up entry."""
    return next(iter(setup_integration.runtime_data.coordinators.values()))
