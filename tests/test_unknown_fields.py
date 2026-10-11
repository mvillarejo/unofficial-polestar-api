from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from polestar_api import grpc as grpc_call
from polestar_api import unknown_fields
from polestar_api.backend import BackendProfile
from polestar_api.codec import encode_double, encode_int32, encode_message, encode_string
from polestar_api.models.battery import Battery, ChargingStatus, GetBatteryResponse
from polestar_api.models.climate import ClimatizationInfo
from polestar_api.models.common import Timestamp
from polestar_api.models.exterior import (
    DoorsStatus,
    DoorStatus,
    ExteriorStatus,
    LockStatus,
    OpenStatus,
)
from polestar_api.models.health import Health
from polestar_api.services.battery import BatteryServiceClient
from polestar_api.services.climate import ClimateServiceClient
from polestar_api.services.exterior import ExteriorServiceClient
from polestar_api.services.health import HealthServiceClient
from polestar_api.services.location import LocationServiceClient

FAKE_ID = "NOT-A-REAL-VIN-123"
SECONDS = 1_760_000_000


@pytest.fixture(autouse=True)
def fresh_record(monkeypatch):
    monkeypatch.setattr(unknown_fields, "UNKNOWN_FIELDS", {})


def _snapshot():
    return unknown_fields.snapshot()


def _values(key):
    return [item["value"] for item in _snapshot()[key]["values"]]


def _timestamp(seconds: int, nanos: int = 0) -> bytes:
    return encode_int32(1, seconds) + encode_int32(2, nanos)


class TestRecorder:
    def test_records_only_fields_outside_known(self):
        unknown_fields.record_fields("x", encode_int32(2, 7) + encode_int32(15, 1), {2})

        assert list(_snapshot()) == ["x:15"]
        entry = _snapshot()["x:15"]
        assert entry["count"] == 1
        assert entry["last"] == 1
        assert entry["more_values"] is False
        assert entry["values"][0]["value"] == 1
        assert datetime.fromisoformat(entry["values"][0]["first_seen"]).utcoffset().total_seconds() == 0

    def test_repeated_value_is_kept_once(self):
        unknown_fields.record_fields("x", {15: 1}, set())
        unknown_fields.record_fields("x", {15: 1}, set())

        entry = _snapshot()["x:15"]
        assert entry["count"] == 2
        assert _values("x:15") == [1]

    def test_distinct_values_are_bounded(self):
        for value in range(25):
            unknown_fields.record_fields("x", {3: value}, set())

        entry = _snapshot()["x:3"]
        assert entry["count"] == 25
        assert entry["last"] == 24
        assert _values("x:3") == list(range(unknown_fields.MAX_VALUES))
        assert entry["more_values"] is True

    def test_number_of_keys_is_bounded(self, monkeypatch):
        monkeypatch.setattr(unknown_fields, "MAX_KEYS", 3)
        unknown_fields.record_fields("x", {n: 1 for n in range(1, 10)}, set())

        assert len(_snapshot()) == 3

    @pytest.mark.parametrize(
        "value",
        [FAKE_ID.encode(), "Ångström road 12".encode(), b"\xff\xfe\x00"],
    )
    def test_strings_and_bytes_keep_only_type_and_length(self, value):
        unknown_fields.record_fields("x", {9: value}, set())

        entry = _snapshot()["x:9"]
        assert entry["last"] == f"bytes[{len(value)}]"
        assert _values("x:9") == [f"bytes[{len(value)}]"]
        assert FAKE_ID not in repr(_snapshot())

    @pytest.mark.parametrize("text", [FAKE_ID, "YSMABCDEFPAPAPAPA"])
    def test_string_from_the_wire_is_not_stored(self, text):
        unknown_fields.record_fields("x", encode_string(4, text), set())

        assert list(_snapshot()) == ["x:4"]
        assert _values("x:4") == [f"bytes[{len(text)}]"]
        assert text not in repr(_snapshot())

    def test_nested_message_is_recorded_with_dotted_paths(self):
        nested = encode_int32(5, 1) + encode_int32(18, 0) + encode_int32(31, 1)
        unknown_fields.record_fields("x", encode_message(46, nested), set())

        assert set(_snapshot()) == {"x:46.5", "x:46.18", "x:46.31"}
        assert _values("x:46.18") == [0]

    def test_nested_timestamp_is_a_single_scalar(self):
        for offset in range(20):
            data = encode_message(47, encode_int32(1, 0) + encode_message(5, _timestamp(SECONDS + offset, 5)))
            unknown_fields.record_fields("x", data, set())

        entry = _snapshot()["x:47.5"]
        assert entry["count"] == 20
        assert _values("x:47.5") == ["timestamp"]
        assert entry["more_values"] is False
        assert entry["last"] == datetime.fromtimestamp(SECONDS + 19, UTC).isoformat()
        assert "x:47.5.1" not in _snapshot()

    def test_small_two_int_message_is_not_a_timestamp(self):
        unknown_fields.record_fields("x", encode_message(8, encode_int32(1, 3)), set())

        assert _values("x:8.1") == [3]

    def test_repeated_field_records_each_item(self):
        unknown_fields.record_fields("x", encode_int32(6, 1) + encode_int32(6, 2), set())

        assert _snapshot()["x:6"]["count"] == 2
        assert _values("x:6") == [1, 2]

    def test_coarse_rounds_floats_and_keeps_nested_as_length(self):
        coordinate = encode_double(1, 18.123456) + encode_double(2, 59.654321)
        data = encode_double(6, 87.66) + encode_message(9, coordinate)
        unknown_fields.record_fields("loc", data, set(), coarse=True)

        assert _values("loc:6") == [87.7]
        assert _values("loc:9") == [f"bytes[{len(coordinate)}]"]
        assert "loc:9.1" not in _snapshot()

    def test_floats_are_stored_as_is_without_coarse(self):
        unknown_fields.record_fields("x", encode_double(6, 87.66), set())

        assert _values("x:6") == [87.66]

    @pytest.mark.parametrize("data", [b"\xff", b"\x0a\x05ab", b"\x08"])
    def test_malformed_data_never_raises(self, data):
        unknown_fields.record_fields("x", data, set())
        unknown_fields.record_message("x", Battery, data)

    def test_record_message_ignores_missing_data(self):
        unknown_fields.record_message("x", Battery, None)

        assert _snapshot() == {}

    def test_record_message_recurses_into_known_nested_schemas(self):
        data = Battery(timestamp=Timestamp(seconds=SECONDS), charge_level=50.0).to_bytes()
        data = data.replace(
            encode_message(1, Timestamp(seconds=SECONDS).to_bytes()),
            encode_message(1, Timestamp(seconds=SECONDS).to_bytes() + encode_int32(3, 4)),
        )
        unknown_fields.record_message("battery", Battery, data)

        assert list(_snapshot()) == ["battery:1.3"]


def _connection():
    async def get_metadata(vin):
        return {}

    return SimpleNamespace(backend=BackendProfile(), channel=None, get_metadata=get_metadata)


@pytest.fixture
def reply(monkeypatch):
    """Serve ``reply.data`` from both unary and stream calls."""
    state = SimpleNamespace(data=b"")

    async def unary(channel, method, request, **kwargs):
        return state.data

    async def stream(channel, method, request, **kwargs):
        yield state.data

    monkeypatch.setattr(grpc_call, "unary_unary", unary)
    monkeypatch.setattr(grpc_call, "unary_stream", stream)
    return state


async def _first(iterator):
    async for item in iterator:
        return item
    return None


class TestBatteryFeed:
    BATTERY = Battery(charge_level=61.0, range_km=300.0, charging_status=ChargingStatus.CHARGING)

    def _response(self, inner: bytes) -> bytes:
        return GetBatteryResponse(vin=FAKE_ID).to_bytes() + encode_message(3, inner)

    @pytest.mark.parametrize("call", ["get_latest", "stream"])
    async def test_f28_is_recorded_and_parsing_is_unchanged(self, reply, call):
        client = BatteryServiceClient(_connection(), FAKE_ID)
        reply.data = self._response(self.BATTERY.to_bytes())
        method = getattr(client, call)
        baseline = await method() if call == "get_latest" else await _first(method())
        assert _snapshot() == {}

        reply.data = self._response(self.BATTERY.to_bytes() + encode_int32(28, 1))
        battery = await method() if call == "get_latest" else await _first(method())

        assert battery == baseline == self.BATTERY
        assert list(_snapshot()) == ["battery:28"]
        assert _values("battery:28") == [1]
        assert FAKE_ID not in repr(_snapshot())


class TestExteriorFeed:
    DIGITAL_TWIN = encode_int32(2, LockStatus.LOCKED) + b"".join(encode_int32(n, 1) for n in range(3, 15))

    def _response(self, inner: bytes) -> bytes:
        return encode_string(2, FAKE_ID) + encode_message(3, inner)

    @pytest.mark.parametrize("call", ["get_latest", "stream"])
    async def test_digital_twin_f15_is_recorded(self, reply, call):
        client = ExteriorServiceClient(_connection(), FAKE_ID)
        method = getattr(client, call)
        reply.data = self._response(self.DIGITAL_TWIN)
        baseline = await method() if call == "get_latest" else await _first(method())
        assert _snapshot() == {}

        reply.data = self._response(self.DIGITAL_TWIN + encode_int32(15, 1))
        status = await method() if call == "get_latest" else await _first(method())

        assert status == baseline
        assert status.central_lock.lock_status == LockStatus.LOCKED
        assert list(_snapshot()) == ["exterior:15"]
        assert _values("exterior:15") == [1]

    async def test_legacy_nested_unknown_is_recorded(self, reply):
        client = ExteriorServiceClient(_connection(), FAKE_ID)
        door = DoorStatus(open_status=OpenStatus.CLOSED).to_bytes()
        status = ExteriorStatus(doors=DoorsStatus(front_left=DoorStatus(open_status=OpenStatus.CLOSED)))
        inner = encode_message(2, encode_message(1, door + encode_int32(9, 2)))
        reply.data = self._response(inner)

        assert await client.get_latest() == status
        assert list(_snapshot()) == ["exterior_legacy:2.1.9"]


class TestLocationFeed:
    COMPACT = encode_double(1, 18.123456) + encode_double(2, 59.654321) + encode_message(3, _timestamp(SECONDS))

    @pytest.mark.parametrize(
        ("outer", "extra", "key", "value"),
        [
            (5, encode_double(5, 23.456), "location:5.5", 23.5),
            (2, encode_int32(6, 2), "location:2.6", 2),
            (5, None, "location:7", 3),
        ],
    )
    @pytest.mark.parametrize("call", ["get_last_known", "get_last_parked", "stream_last_known", "stream_last_parked"])
    async def test_nested_shapes_record_extra_fields(self, reply, call, outer, extra, key, value):
        client = LocationServiceClient(_connection(), FAKE_ID)
        method = getattr(client, call)
        reply.data = encode_message(outer, self.COMPACT)
        baseline = await method() if call.startswith("get") else await _first(method())

        if extra is None:
            reply.data = encode_message(outer, self.COMPACT) + encode_int32(7, 3)
        else:
            reply.data = encode_message(outer, self.COMPACT + extra)
        location = await method() if call.startswith("get") else await _first(method())

        assert location == baseline
        assert location.coordinate.latitude == 59.654321
        assert list(_snapshot()) == [key]
        assert _values(key) == [value]

    async def test_flat_shape_records_extra_fields(self, reply):
        client = LocationServiceClient(_connection(), FAKE_ID)
        flat = encode_double(2, 18.123456) + encode_double(3, 59.654321) + encode_int32(4, SECONDS)
        reply.data = flat
        baseline = await client.get_last_known()

        reply.data = flat + encode_double(8, 271.04)

        assert await client.get_last_known() == baseline
        assert list(_snapshot()) == ["location:8"]
        assert _values("location:8") == [271.0]

    async def test_coordinates_never_reach_the_record(self, reply):
        client = LocationServiceClient(_connection(), FAKE_ID)
        stray = encode_message(9, encode_double(1, 18.123456) + encode_double(2, 59.654321))
        reply.data = encode_message(5, self.COMPACT + stray)

        await client.get_last_known()

        text = repr(_snapshot())
        assert "18.1" not in text and "59.6" not in text
        assert _values("location:5.9") == [f"bytes[{len(stray) - 2}]"]


class TestClimateFeed:
    PAYLOAD = bytes.fromhex(
        "0a0c08d28eddd30610c8e7828a011001181e30013d0000dc41450000b841"
        "48015001580160016801720c089086ddd30610c0bcb1dc0178028201"
        "0c08cd94ddd30610e8bc8dd102"
    )

    @pytest.mark.parametrize("call", ["get_latest", "stream"])
    async def test_digital_twin_unknown_is_recorded(self, reply, call):
        client = ClimateServiceClient(_connection(), FAKE_ID)
        method = getattr(client, call)
        reply.data = encode_message(3, self.PAYLOAD)
        baseline = await method() if call == "get_latest" else await _first(method())
        assert _snapshot() == {}

        reply.data = encode_message(3, self.PAYLOAD + encode_int32(5, 2))
        info = await method() if call == "get_latest" else await _first(method())

        assert info == baseline
        assert list(_snapshot()) == ["climate:5"]

    async def test_legacy_unknown_is_recorded(self, reply):
        client = ClimateServiceClient(_connection(), FAKE_ID)
        reply.data = encode_message(3, encode_int32(1, 1) + encode_int32(5, 9))

        info = await client.get_latest()

        assert info == ClimatizationInfo.from_bytes(encode_int32(1, 1))
        assert list(_snapshot()) == ["climate_legacy:5"]


class TestHealthFeed:
    F46 = b"".join(encode_int32(n, v) for n, v in [(5, 1), (6, 1), (11, 1), (14, 1), (18, 0), (22, 1), (27, 1), (28, 1), (31, 1), (38, 1)])
    F47 = b"".join(encode_int32(n, 0) for n in (1, 2, 3, 4)) + encode_message(5, _timestamp(SECONDS, 7)) + encode_int32(6, 0) + encode_int32(7, 0)

    def _inner(self) -> bytes:
        return Health(days_to_service=200, front_left_tyre_pressure_kpa=250.0).to_bytes()

    @pytest.mark.parametrize("call", ["get_latest", "stream"])
    async def test_f46_and_f47_are_recorded_with_dotted_paths(self, reply, call):
        client = HealthServiceClient(_connection(), FAKE_ID)
        method = getattr(client, call)
        reply.data = encode_message(3, self._inner())
        baseline = await method() if call == "get_latest" else await _first(method())
        assert _snapshot() == {}

        reply.data = encode_message(3, self._inner() + encode_message(46, self.F46) + encode_message(47, self.F47))
        health = await method() if call == "get_latest" else await _first(method())

        assert health == baseline
        assert set(_snapshot()) == {
            *(f"health:46.{n}" for n in (5, 6, 11, 14, 18, 22, 27, 28, 31, 38)),
            *(f"health:47.{n}" for n in (1, 2, 3, 4, 5, 6, 7)),
        }
        assert _values("health:46.18") == [0]
        assert _values("health:47.5") == ["timestamp"]
