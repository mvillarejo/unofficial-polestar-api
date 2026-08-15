"""Diagnostics support for the Polestar integration."""

from __future__ import annotations

import time
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_VIN
from .coordinator import PolestarConfigEntry, PolestarCoordinator

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_VIN,
    "username",
    "internal_id",
    "registration_no",
    "id",
    "location_id",
    "location_alias",
    "latitude",
    "longitude",
    "altitude",
}


def _serialize(value: Any) -> Any:
    """Convert models to JSON-friendly structures, keeping enum names readable."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _serialize(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(item) for item in value]
    return value


def _stream_diagnostics(coordinator: PolestarCoordinator) -> dict[str, Any]:
    """Report liveness of the long-lived streaming subscriptions."""
    now = time.monotonic()
    streams: dict[str, Any] = {}
    for attr in sorted(coordinator._STREAMS):
        task = coordinator._stream_tasks.get(attr)
        last_data = coordinator._stream_last_data.get(attr)
        info: dict[str, Any] = {
            "started": task is not None,
            "running": task is not None and not task.done(),
            "supported": attr not in coordinator._unsupported_streams,
            "seconds_since_last_data": (
                round(now - last_data, 1) if last_data is not None else None
            ),
        }
        if task is not None and task.done() and not task.cancelled():
            err = task.exception()
            if err is not None:
                info["failed_with"] = type(err).__name__
        streams[attr] = info
    return streams


def _coordinator_diagnostics(coordinator: PolestarCoordinator) -> dict[str, Any]:
    """Dump one vehicle's coordinator state."""
    return {
        "model_name": coordinator.vehicle.model_name,
        "model_year": coordinator.vehicle.model_year,
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval is not None
            else None
        ),
        "last_update_success": coordinator.last_update_success,
        "poll_count": coordinator._poll_count,
        "all_fetches_failed": coordinator._all_fetches_failed,
        "unsupported_fetches": sorted(coordinator._unsupported_fetches),
        "unsupported_streams": sorted(coordinator._unsupported_streams),
        "unsupported_commands": sorted(coordinator._unsupported_commands),
        "streams": _stream_diagnostics(coordinator),
        "data": async_redact_data(_serialize(coordinator.data), TO_REDACT),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PolestarConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "demo_mode": data.api is None,
        "vehicles": [
            _coordinator_diagnostics(coordinator)
            for coordinator in data.coordinators.values()
        ],
    }
