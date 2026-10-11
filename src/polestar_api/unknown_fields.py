"""Passive record of protobuf fields that the parsers do not map.

Strings and bytes are never stored, only their type and length, so the record
cannot hold a VIN or any other identifier.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from .codec import decode, decode_varint
from .wire import ProtoMessage, _unwrap_optional

_LOGGER = logging.getLogger(__name__)

MAX_VALUES = 10
MAX_KEYS = 200
_MAX_DEPTH = 4
_TIMESTAMP_RANGE = range(1_000_000_000, 10_000_000_000)

# Unmapped fields per "label:path", since import.
UNKNOWN_FIELDS: dict[str, dict[str, Any]] = {}


def record_fields(
    label: str,
    data: bytes | dict,
    known: Collection[int],
    *,
    prefix: str = "",
    coarse: bool = False,
) -> None:
    """Record the fields of a raw message that are not in ``known``.

    ``coarse`` is for location messages: floats are rounded to one decimal and
    unknown nested messages are kept as a length only, so no coordinate pair
    can be recorded.
    """
    try:
        raw = decode(data) if isinstance(data, bytes) else data
        for num, value in raw.items():
            if num not in known:
                _record(label, f"{prefix}{num}", value, coarse, 0)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not record unknown fields for %s", label, exc_info=True)


def record_message(label: str, cls: type[ProtoMessage], data: bytes | None) -> None:
    """Record the fields of ``data`` that ``cls`` and its nested schemas do not map."""
    try:
        if isinstance(data, bytes):
            _walk(label, cls, data, "")
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not record unknown fields for %s", label, exc_info=True)


def snapshot() -> dict[str, dict[str, Any]]:
    """Return the record as JSON-friendly data, sorted by key."""
    return {
        key: {
            "count": entry["count"],
            "last": entry["last"],
            "values": [
                {"value": value, "first_seen": seen}
                for value, seen in entry["values"].items()
            ],
            "more_values": entry["more_values"],
        }
        for key, entry in sorted(UNKNOWN_FIELDS.items())
    }


def _walk(label: str, cls: type[ProtoMessage], data: bytes, prefix: str) -> None:
    cls._ensure_maps()
    for num, value in decode(data).items():
        known = cls._decode_map.get(num)
        if known is None:
            _record(label, f"{prefix}{num}", value, False, 0)
            continue
        name, wire_type = known
        if wire_type == "message" and isinstance(value, bytes):
            nested = _unwrap_optional(cls._type_hints[name])
            _walk(label, nested, value, f"{prefix}{num}.")


def _record(label: str, path: str, value: Any, coarse: bool, depth: int) -> None:
    if isinstance(value, list):
        for item in value:
            _record(label, path, item, coarse, depth)
        return
    if isinstance(value, bytes):
        nested = _as_message(value) if depth < _MAX_DEPTH and not coarse else None
        if nested is not None:
            if _is_timestamp(nested):
                seconds = nested[1]
                _store(label, path, "timestamp", datetime.fromtimestamp(seconds, UTC).isoformat())
                return
            for num, item in nested.items():
                _record(label, f"{path}.{num}", item, coarse, depth + 1)
            return
        summary = f"bytes[{len(value)}]"
        _store(label, path, summary, summary)
        return
    if isinstance(value, float):
        value = round(value, 1) if coarse else value
    elif not isinstance(value, int):
        value = type(value).__name__
    _store(label, path, value, value)


def _store(label: str, path: str, value: Any, last: Any) -> None:
    key = f"{label}:{path}"
    entry = UNKNOWN_FIELDS.get(key)
    if entry is None:
        if len(UNKNOWN_FIELDS) >= MAX_KEYS:
            return
        entry = UNKNOWN_FIELDS[key] = {
            "count": 0,
            "last": None,
            "values": {},
            "more_values": False,
        }
    entry["count"] += 1
    entry["last"] = last
    if value in entry["values"]:
        return
    if len(entry["values"]) >= MAX_VALUES:
        entry["more_values"] = True
        return
    entry["values"][value] = datetime.now(UTC).isoformat(timespec="seconds")


def _as_message(data: bytes) -> dict | None:
    """Decode ``data`` as a nested message, or ``None`` if it looks like text."""
    if not data or _is_text(data) or not _is_wellformed(data):
        return None
    return decode(data)


def _is_text(data: bytes) -> bool:
    try:
        return data.decode("utf-8").isprintable()
    except UnicodeDecodeError:
        return False


def _is_wellformed(data: bytes) -> bool:
    pos = 0
    try:
        while pos < len(data):
            tag, pos = decode_varint(data, pos)
            wire_type = tag & 0x07
            if tag >> 3 == 0:
                return False
            if wire_type == 0:
                _, pos = decode_varint(data, pos)
            elif wire_type == 1:
                pos += 8
            elif wire_type == 2:
                length, pos = decode_varint(data, pos)
                pos += length
            elif wire_type == 5:
                pos += 4
            else:
                return False
    except IndexError:
        return False
    return pos == len(data)


def _is_timestamp(fields: dict) -> bool:
    return (
        set(fields) <= {1, 2}
        and isinstance(fields.get(1), int)
        and fields[1] in _TIMESTAMP_RANGE
        and isinstance(fields.get(2, 0), int)
    )
