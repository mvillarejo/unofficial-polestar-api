"""Config and options flow coverage (Bronze: config-flow-test-coverage)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.polestar.const import (
    CONF_DEMO,
    CONF_ENABLE_STREAMS,
    CONF_UPDATE_INTERVAL,
    CONF_VIN,
    DOMAIN,
)
from polestar_api.exceptions import AuthError

from .conftest import TEST_VIN

CREDENTIALS = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "hunter2"}


@pytest.fixture
def flow_api(mock_vehicle) -> MagicMock:
    """A PolestarApi used inside the config flow."""
    api = MagicMock()
    api.async_init = AsyncMock(return_value=None)
    api.get_vehicles = AsyncMock(return_value=[mock_vehicle])
    api.close = AsyncMock(return_value=None)
    return api


def _patch_flow_api(api: MagicMock):
    return patch("custom_components.polestar.config_flow.PolestarApi", return_value=api)


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_full_user_flow(hass: HomeAssistant, flow_api, mock_api) -> None:
    """Credentials, then vehicle picker, then an entry."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with _patch_flow_api(flow_api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "vehicle"

    with patch("custom_components.polestar.PolestarApi", return_value=mock_api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VIN: TEST_VIN, CONF_UPDATE_INTERVAL: 120}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VIN] == TEST_VIN
    assert result["options"][CONF_UPDATE_INTERVAL] == 120
    assert result["result"].unique_id == TEST_VIN


async def test_invalid_auth(hass: HomeAssistant, flow_api) -> None:
    """Bad credentials surface as a recoverable form error."""
    flow_api.async_init.side_effect = AuthError("nope")
    result = await _start(hass)

    with _patch_flow_api(flow_api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_cannot_connect(hass: HomeAssistant, flow_api) -> None:
    """A transport failure is distinguished from an auth failure."""
    flow_api.async_init.side_effect = OSError("no route to host")
    result = await _start(hass)

    with _patch_flow_api(flow_api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_recovers_after_invalid_auth(hass: HomeAssistant, flow_api, mock_api) -> None:
    """The user can correct the password without restarting the flow."""
    flow_api.async_init.side_effect = AuthError("nope")
    result = await _start(hass)

    with _patch_flow_api(flow_api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
        assert result["errors"] == {"base": "invalid_auth"}

        flow_api.async_init.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "vehicle"


async def test_guest_account_falls_back_to_manual_vin(
    hass: HomeAssistant, flow_api, mock_api
) -> None:
    """An account with no listed vehicles gets the manual VIN step."""
    flow_api.get_vehicles.return_value = []
    result = await _start(hass)

    with _patch_flow_api(flow_api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["step_id"] == "guest_vin"

    with patch("custom_components.polestar.PolestarApi", return_value=mock_api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VIN: TEST_VIN.lower(), CONF_UPDATE_INTERVAL: 300}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VIN] == TEST_VIN


async def test_demo_flow(hass: HomeAssistant) -> None:
    """Demo mode skips authentication entirely."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDENTIALS, CONF_DEMO: True}
    )
    assert result["step_id"] == "demo_vin"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_VIN: "DEMO000000000001"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEMO] is True


async def test_duplicate_vin_aborts(
    hass: HomeAssistant, setup_integration, flow_api
) -> None:
    """The same VIN cannot be configured twice."""
    result = await _start(hass)

    with _patch_flow_api(flow_api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_VIN: TEST_VIN, CONF_UPDATE_INTERVAL: 120}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(hass: HomeAssistant, setup_integration, flow_api, mock_api) -> None:
    """Re-entering the password updates the entry and reloads it."""
    result = await setup_integration.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with (
        _patch_flow_api(flow_api),
        patch("custom_components.polestar.PolestarApi", return_value=mock_api),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert setup_integration.data[CONF_PASSWORD] == "new-password"


async def test_reauth_flow_invalid_auth(
    hass: HomeAssistant, setup_integration, flow_api
) -> None:
    """A still-wrong password keeps the user in the reauth form."""
    flow_api.async_init.side_effect = AuthError("still wrong")
    result = await setup_integration.start_reauth_flow(hass)

    with _patch_flow_api(flow_api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "still-wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_flow(hass: HomeAssistant, setup_integration, mock_api) -> None:
    """The options flow exposes the base interval and the streams toggle."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    with patch("custom_components.polestar.PolestarApi", return_value=mock_api):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_UPDATE_INTERVAL: 300, CONF_ENABLE_STREAMS: True},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options[CONF_UPDATE_INTERVAL] == 300
    assert setup_integration.options[CONF_ENABLE_STREAMS] is True
