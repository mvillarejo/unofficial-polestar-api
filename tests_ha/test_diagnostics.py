"""Diagnostics: the api_calls block that measures load on the Polestar backend."""

from __future__ import annotations

import json
import time
from collections import Counter

import pytest
from homeassistant.core import HomeAssistant

from custom_components.polestar.diagnostics import async_get_config_entry_diagnostics
from polestar_api import grpc as grpc_call
from polestar_api import unknown_fields
from polestar_api.codec import encode_double, encode_int32, encode_message, encode_string

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


async def test_unknown_fields_reports_unmapped_fields_without_identifiers(
    hass: HomeAssistant, setup_integration, monkeypatch
) -> None:
    monkeypatch.setattr(unknown_fields, "UNKNOWN_FIELDS", {})
    unknown_fields.record_fields(
        "exterior", encode_int32(15, 1) + encode_string(17, TEST_VIN), set(range(2, 15))
    )
    coordinate = encode_double(1, 18.123456) + encode_double(2, 59.654321)
    unknown_fields.record_fields(
        "location",
        encode_double(6, 87.66) + encode_message(9, coordinate),
        {2, 3, 4},
        coarse=True,
    )

    diag = await async_get_config_entry_diagnostics(hass, setup_integration)

    block = diag["unknown_fields"]
    assert list(block) == ["exterior:15", "exterior:17", "location:6", "location:9"]
    assert block["exterior:15"]["count"] == 1
    assert block["exterior:15"]["last"] == 1
    assert block["exterior:15"]["values"][0]["value"] == 1
    assert block["exterior:17"]["last"] == f"bytes[{len(TEST_VIN)}]"
    assert block["location:6"]["last"] == 87.7
    text = json.dumps(block)
    assert TEST_VIN not in text
    assert "18.1" not in text
    assert "59.6" not in text


async def test_unknown_fields_empty_by_default(
    hass: HomeAssistant, setup_integration, monkeypatch
) -> None:
    monkeypatch.setattr(unknown_fields, "UNKNOWN_FIELDS", {})

    diag = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert diag["unknown_fields"] == {}
