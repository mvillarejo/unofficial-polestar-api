"""Diagnostics: the api_calls block that measures load on the Polestar backend."""

from __future__ import annotations

import time
from collections import Counter

import pytest
from homeassistant.core import HomeAssistant

from custom_components.polestar.diagnostics import async_get_config_entry_diagnostics
from polestar_api import grpc as grpc_call

from .conftest import TEST_VIN


async def test_api_calls_reports_counts_and_daily_rate(
    hass: HomeAssistant, setup_integration, monkeypatch
) -> None:
    started = time.time() - 12 * 60 * 60
    monkeypatch.setattr(
        grpc_call, "CALL_COUNTS", Counter({"/svc.B/Get": 30, "/svc.A/Get": 20})
    )
    monkeypatch.setattr(grpc_call, "CALL_COUNTS_SINCE", started)

    diag = await async_get_config_entry_diagnostics(hass, setup_integration)

    api_calls = diag["api_calls"]
    assert api_calls["total"] == 50
    assert list(api_calls["per_method"]) == ["/svc.A/Get", "/svc.B/Get"]
    assert api_calls["per_method"]["/svc.B/Get"] == 30
    assert api_calls["since"].startswith(time.strftime("%Y-%m-%d", time.gmtime(started)))
    assert api_calls["per_day_rate"] == pytest.approx(100, rel=0.01)


async def test_api_calls_with_no_traffic(
    hass: HomeAssistant, setup_integration, monkeypatch
) -> None:
    monkeypatch.setattr(grpc_call, "CALL_COUNTS", Counter())
    monkeypatch.setattr(grpc_call, "CALL_COUNTS_SINCE", time.time())

    diag = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert diag["api_calls"]["total"] == 0
    assert diag["api_calls"]["per_method"] == {}
    assert diag["api_calls"]["per_day_rate"] == 0


async def test_diagnostics_keep_redacting_the_vin(
    hass: HomeAssistant, setup_integration
) -> None:
    diag = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert TEST_VIN not in repr(diag)
