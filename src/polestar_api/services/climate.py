"""Parking climatization service — climate status."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .. import grpc as grpc_call
from ..codec import decode
from ..models.climate import (
    ACTIVE_RUNNING_STATUSES,
    ClimatizationInfo,
    ClimatizationRequestType,
    ClimatizationRunningStatus,
    HeatOrCoolAction,
)
from ..models.climatization import HeatingIntensity
from ..models.common import VehicleRequest

if TYPE_CHECKING:
    from ..connection import GrpcConnection

_RESPONSE_SCHEMA = {3: ("climate", "message")}


class ClimateServiceClient:
    """Parking climatization status service."""

    def __init__(self, connection: GrpcConnection, vin: str) -> None:
        self._connection = connection
        self._vin = vin

    @property
    def _svc(self) -> str:
        return self._connection.backend.climate_svc

    @staticmethod
    def _map_running_status(value: int | None) -> ClimatizationRunningStatus:
        mapping = {
            1: ClimatizationRunningStatus.ACTIVE,
            2: ClimatizationRunningStatus.IDLE,
            3: ClimatizationRunningStatus.START_ATTEMPT,
        }
        return mapping.get(value, ClimatizationRunningStatus.UNDEFINED)

    @staticmethod
    def _map_request_type(value: int | None) -> ClimatizationRequestType:
        mapping = {
            1: ClimatizationRequestType.NOW_FROM_HMI,
            2: ClimatizationRequestType.NOW_FROM_REMOTE,
            3: ClimatizationRequestType.TIMER,
            4: ClimatizationRequestType.NO_REQUEST,
        }
        return mapping.get(value, ClimatizationRequestType.UNDEFINED)

    @staticmethod
    def _map_seat_heating(value: int | None) -> HeatingIntensity | None:
        if value is None:
            return None
        try:
            return HeatingIntensity(value)
        except ValueError:
            return None

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        """Decode a nested google.protobuf.Timestamp into an aware datetime."""
        if not isinstance(value, bytes):
            return None
        try:
            fields = decode(value)
        except Exception:  # noqa: BLE001
            return None
        seconds = fields.get(1)
        if not isinstance(seconds, int):
            return None
        nanos = fields.get(2)
        micros = nanos // 1000 if isinstance(nanos, int) else 0
        try:
            return datetime.fromtimestamp(seconds, UTC).replace(microsecond=micros)
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _infer_heat_or_cool_action(raw: dict) -> HeatOrCoolAction:
        if raw.get(6):
            return HeatOrCoolAction.VENTILATION_ONLY

        current_temp = raw.get(7)
        requested_temp = raw.get(8)
        if not isinstance(current_temp, (int, float)) or not isinstance(requested_temp, (int, float)):
            return HeatOrCoolAction.UNDEFINED
        if requested_temp > current_temp:
            return HeatOrCoolAction.HEATING
        if requested_temp < current_temp:
            return HeatOrCoolAction.COOLING
        return HeatOrCoolAction.NOT_REQUIRED

    @staticmethod
    def _remaining_at_report(
        running_status: ClimatizationRunningStatus,
        end_time: datetime | None,
        reported_at: datetime | None,
    ) -> int:
        """Minutes left as of the car's own report timestamp.

        Field 3 of the digital twin message is the configured session length
        (always 30), not a countdown — the countdown has to be derived from the
        end timestamp.
        """
        if end_time is None or reported_at is None:
            return 0
        if running_status not in ACTIVE_RUNNING_STATUSES:
            return 0
        return max(0, round((end_time - reported_at).total_seconds() / 60))

    @classmethod
    def _parse_digital_twin(cls, climate_bytes: bytes) -> ClimatizationInfo:
        raw = decode(climate_bytes)
        current_temp = raw.get(7)
        target_temp = raw.get(8)
        running_status = cls._map_running_status(raw.get(2))
        reported_at = cls._parse_timestamp(raw.get(1))
        start_time = cls._parse_timestamp(raw.get(14))
        end_time = cls._parse_timestamp(raw.get(16))
        return ClimatizationInfo(
            running_status=running_status,
            request_type=cls._map_request_type(raw.get(15)),
            time_remaining=cls._remaining_at_report(
                running_status, end_time, reported_at
            ),
            duration_minutes=int(raw.get(3, 0) or 0) or None,
            start_time=start_time,
            end_time=end_time,
            reported_at=reported_at,
            heat_or_cool_action=cls._infer_heat_or_cool_action(raw),
            current_temperature_celsius=float(current_temp) if isinstance(current_temp, (int, float)) else None,
            target_temperature_celsius=float(target_temp) if isinstance(target_temp, (int, float)) else None,
            front_left_seat=cls._map_seat_heating(raw.get(9)),
            front_right_seat=cls._map_seat_heating(raw.get(10)),
            rear_right_seat=cls._map_seat_heating(raw.get(11)),
            rear_left_seat=cls._map_seat_heating(raw.get(12)),
            steering_wheel=cls._map_seat_heating(raw.get(13)),
        )

    @classmethod
    def _parse(cls, data: bytes) -> ClimatizationInfo | None:
        raw = decode(data, _RESPONSE_SCHEMA)
        if raw.get("climate"):
            climate_bytes = raw["climate"]
            payload = decode(climate_bytes)
            if isinstance(payload.get(1), bytes):
                return cls._parse_digital_twin(climate_bytes)
            return ClimatizationInfo.from_bytes(climate_bytes)
        return None

    async def get_latest(self) -> ClimatizationInfo | None:
        request = VehicleRequest(vin=self._vin)
        metadata = await self._connection.get_metadata(self._vin)
        data = await grpc_call.unary_unary(
            self._connection.channel,
            f"{self._svc}/GetLatestParkingClimatization",
            request.to_bytes(),
            metadata=metadata,
        )
        return self._parse(data)

    async def stream(self) -> AsyncIterator[ClimatizationInfo]:
        """Stream parking climatization status updates."""
        request = VehicleRequest(vin=self._vin)
        metadata = await self._connection.get_metadata(self._vin)
        async for data in grpc_call.unary_stream(
            self._connection.channel,
            f"{self._svc}/GetParkingClimatization",
            request.to_bytes(),
            metadata=metadata,
        ):
            status = self._parse(data)
            if status is not None:
                yield status
