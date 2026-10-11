"""Data update coordinators for Polestar vehicles.

``PolestarCoordinator`` owns the single ``PolestarVehicleData`` snapshot,
every remote command, and the entity listener bus. It polls every attribute
in ``_FETCH_ATTRS`` together on one timer whose interval adapts to the car:
``CONF_UPDATE_INTERVAL`` (default 30 minutes) while it is idle, a short
constant while it is active (see ``car_is_active``). A few persistent gRPC
subscriptions (``STREAM_METHODS``) sit on top as the responsiveness
mechanism — the poll is the freshness guarantee, streams are what make
battery, locks, climate and location update within seconds.

Entities subscribe to the coordinator directly, so they see the complete
snapshot regardless of whether a given attribute last changed via the poll
or a stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import MISSING, dataclass, field, fields, replace
from datetime import time as dt_time, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from grpclib.const import Status as GrpcStatus
from grpclib.exceptions import GRPCError
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from polestar_api.exceptions import AuthError, TokenExpiredError
from polestar_api.models.availability import Availability, UsageMode
from polestar_api.models.battery import Battery, ChargingStatus
from polestar_api.models.charge_location import ChargeLocation
from polestar_api.models.charging import (
    AmpLimitResponse,
    BatteryChargeTimer,
    ChargeTargetLevelSettingType,
    ChargeTimerResponse,
    DailyTime,
    TimeZoneOffset,
    TargetSocResponse,
)
from polestar_api.models.climate import ClimatizationInfo
from polestar_api.models.climatization import HeatingIntensity
from polestar_api.models.common import Location, ResponseStatusCode
from polestar_api.models.connectivity import ConnectivityInfo
from polestar_api.models.dashboard import DashboardStatus
from polestar_api.models.exterior import ExteriorStatus, OpenStatus
from polestar_api.models.health import Health
from polestar_api.models.invocation import InvocationStatus
from polestar_api.models.odometer import OdometerStatus
from polestar_api.models.ota import CarSoftwareInfo, Scheduler, SoftwareState
from polestar_api.models.parking_climate_timer import (
    ParkingClimateTimer,
    ParkingClimateTimerSettings,
)
from polestar_api.models.precleaning import PreCleaningInfo
from polestar_api.models.weather import WeatherReport

from .const import (
    ACTIVE_UPDATE_INTERVAL_POLLING,
    ACTIVE_UPDATE_INTERVAL_STREAMING,
    COMMAND_ACTIVE_WINDOW,
    CONF_ENABLE_STREAMS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_CLIMATE_TEMPERATURE,
    DEFAULT_ENABLE_STREAMS,
    DEFAULT_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    STREAM_MAX_RETRIES,
    STREAM_MIN_RESUBSCRIBE_INTERVAL,
    STREAM_RETRY_DELAY,
)
from .utils import local_utc_offset_minutes

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from polestar_api import PolestarApi
    from polestar_api.vehicle import Vehicle

_LOGGER = logging.getLogger(__name__)
# Measured against a real car: the cloud reflects a lock/unlock roughly 3-30s
# after the command is accepted, and a climatisation start can take well over a
# minute. The old schedule stopped at 33s, so the entity fell back to the
# pre-command snapshot before the car had reported anything, which is what made
# a command look like it had been silently undone.
_POST_COMMAND_REFRESH_DELAYS: tuple[int, ...] = (3, 5, 10, 15, 30, 60)
# When a running stream already covers the attribute it reports the change
# within seconds on its own, so the refreshes are only a fallback.
_STREAMED_POST_COMMAND_REFRESH_DELAYS: tuple[int, ...] = (10, 60)
# Endpoints that answered UNIMPLEMENTED are skipped on scheduled polls, but
# re-probed this often (seconds) in case the backend starts serving them again.
# Elapsed time rather than a poll count, so it holds if the poll interval changes.
_UNSUPPORTED_REPROBE_INTERVAL = 24 * 60 * 60
# The P4 answers charge-location reads with INVALID_ARGUMENT on every call.
# One such answer can be a transient backend hiccup, so only this many in a row
# mark the endpoint unsupported like UNIMPLEMENTED does.
_INVALID_ARGUMENT_UNSUPPORTED_AFTER = 3
_FETCH_TIMEOUT = 15
_COMMAND_INVOCATION_SUCCESS = {
    InvocationStatus.SENT,
    InvocationStatus.DELIVERED,
    InvocationStatus.SUCCESS,
}
_COMMAND_RESPONSE_STATUS_SUCCESS = {
    ResponseStatusCode.SUCCESS,
    ResponseStatusCode.WARNING,
}
# Someone is in or at the car. INACTIVE/ABANDONED/ENGINE_OFF read as parked.
_IN_USE_MODES = {
    UsageMode.CONVENIENCE,
    UsageMode.ACTIVE,
    UsageMode.DRIVING,
    UsageMode.ENGINE_ON,
}
_CLOSED = {OpenStatus.UNSPECIFIED, OpenStatus.CLOSED}


@dataclass
class ClimateCommandPreferences:
    """Seat and steering-wheel heating chosen in HA for climate start commands.

    The target temperature deliberately does *not* live here. It is resolved
    from live car data by ``PolestarCoordinator.climate_target_temperature``
    so that the value the number entity shows and the value a start command
    sends can never drift apart.
    """

    front_left_seat: HeatingIntensity = HeatingIntensity.UNSPECIFIED
    front_right_seat: HeatingIntensity = HeatingIntensity.UNSPECIFIED
    rear_left_seat: HeatingIntensity = HeatingIntensity.UNSPECIFIED
    rear_right_seat: HeatingIntensity = HeatingIntensity.UNSPECIFIED
    steering_wheel: HeatingIntensity = HeatingIntensity.UNSPECIFIED


@dataclass
class PolestarVehicleData:
    """Snapshot of all polled vehicle data."""

    battery: Battery | None = None
    exterior: ExteriorStatus | None = None
    location: Location | None = None
    parked_location: Location | None = None
    climate: ClimatizationInfo | None = None
    dashboard: DashboardStatus | None = None
    health: Health | None = None
    availability: Availability | None = None
    connectivity: ConnectivityInfo | None = None
    odometer: OdometerStatus | None = None
    precleaning: PreCleaningInfo | None = None
    weather: WeatherReport | None = None
    software: CarSoftwareInfo | None = None
    ota_schedule: Scheduler | None = None
    target_soc: TargetSocResponse | None = None
    amp_limit: AmpLimitResponse | None = None
    charge_timer: ChargeTimerResponse | None = None
    charge_locations: list[ChargeLocation] = field(default_factory=list)
    current_charge_location: dict[str, Any] = field(default_factory=dict)
    climate_timers: list[ParkingClimateTimer] = field(default_factory=list)
    climate_timer_settings: ParkingClimateTimerSettings | None = None


_FETCH_ATTRS: dict[str, str] = {
    "battery": "get_battery",
    "exterior": "get_exterior",
    "location": "get_location",
    "parked_location": "get_parked_location",
    "odometer": "get_odometer",
    "climate": "get_climate",
    "dashboard": "get_dashboard",
    "health": "get_health",
    "availability": "get_availability",
    "connectivity": "get_connectivity",
    "precleaning": "get_precleaning",
    "weather": "get_weather",
    "software": "get_software_info",
    "ota_schedule": "get_ota_schedule",
    "target_soc": "get_target_soc",
    "amp_limit": "get_amp_limit",
    "charge_timer": "get_charge_timer",
    "charge_locations": "get_charge_locations",
    "current_charge_location": "is_at_charge_location",
    "climate_timers": "get_climate_timers",
    "climate_timer_settings": "get_climate_timer_settings",
}

# Live subscriptions, deliberately few. Measured on a Polestar 4: the server
# closes every subscription cleanly after ~120s and replays the latest snapshot
# on resubscribe, so each stream that is kept open costs ~720 calls a day
# whether or not anything changed. Only what is worth that price streams; every
# other attribute is poll-only. Battery/charging, locks/doors/windows and
# climate stream whenever streams are enabled, so climate started from the app
# or a departure timer shows up while the car is parked; location only while
# the car is active (see PolestarCoordinator.car_is_active).
ALWAYS_ON_STREAMS: dict[str, str] = {
    "battery": "stream_battery",
    "exterior": "stream_exterior",
    "climate": "stream_climate",
}
ACTIVE_ONLY_STREAMS: dict[str, str] = {
    "location": "stream_location",
}
STREAM_METHODS: dict[str, str] = {**ALWAYS_ON_STREAMS, **ACTIVE_ONLY_STREAMS}

_ATTR_FIELDS = {spec.name: spec for spec in fields(PolestarVehicleData)}


def _attr_default(attr: str) -> Any:
    """Return the empty PolestarVehicleData value for an attribute."""
    spec = _ATTR_FIELDS[attr]
    if spec.default_factory is not MISSING:
        return spec.default_factory()
    return spec.default


class PolestarCoordinator(DataUpdateCoordinator[PolestarVehicleData]):
    """Owns the vehicle snapshot, commands, listeners, and the poll timer."""

    def __init__(
        self,
        hass: HomeAssistant,
        vehicle: Vehicle,
        entry: ConfigEntry,
    ) -> None:
        base = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        base = max(MIN_UPDATE_INTERVAL, min(base, MAX_UPDATE_INTERVAL))
        self.idle_interval = timedelta(seconds=base)
        super().__init__(
            hass,
            _LOGGER,
            name=f"Polestar {vehicle.vin}",
            update_interval=self.idle_interval,
            config_entry=entry,
            always_update=False,
        )
        self.vehicle = vehicle
        self.climate_preferences = ClimateCommandPreferences()
        self.data = PolestarVehicleData()
        self._climate_temperature_override: float | None = None
        self._installed_version_cache: str | None = None
        self._stream_tasks: dict[str, asyncio.Task[None]] = {}
        self._unsupported_streams: set[str] = set()
        self._unsupported_commands: set[str] = set()
        self._unsupported_fetches: set[str] = set()
        self._invalid_argument_counts: dict[str, int] = {}
        self._generation = 0
        self._applied_generations: dict[str, int] = {}
        self._poll_count = 0
        self._last_unsupported_probe: float | None = None
        self._active = False
        self._last_command_at: float | None = None

    def next_fetch_generation(self) -> int:
        """Claim the next fetch sequence number.

        Callers take one *before* awaiting their fetch and hand it back to
        ``async_apply_values``, so results are ordered by when they were asked
        for rather than by when they happened to come back.
        """
        self._generation += 1
        return self._generation

    def async_apply_values(
        self, values: dict[str, Any], *, generation: int | None = None
    ) -> None:
        """Merge fetched/streamed values into the snapshot and notify entities.

        Tier polls, post-command refreshes and live streams all write here
        concurrently, and they finish in whatever order the network decides.
        *generation* is the sequence number claimed when the fetch started; an
        attribute is only overwritten by a fetch newer than the one that last
        wrote it, so a slow poll cannot resurrect pre-command state on top of a
        faster post-command refresh. Streams and other unsequenced callers
        claim a generation now, which correctly makes them newer than anything
        still in flight.

        Entities are only notified when a value actually changed: a stream
        replaying its snapshot on every resubscribe would otherwise rewrite
        every entity's state each time.
        """
        if self._apply_values(values, generation):
            self.async_update_listeners()

    def _apply_values(self, values: dict[str, Any], generation: int | None) -> bool:
        """Merge values into the snapshot; return whether anything changed."""
        if not values:
            return False
        if generation is None:
            generation = self.next_fetch_generation()

        fresh = {
            attr: value
            for attr, value in values.items()
            if generation > self._applied_generations.get(attr, 0)
        }
        for attr in fresh:
            self._applied_generations[attr] = generation
        changed = {
            attr: value for attr, value in fresh.items() if getattr(self.data, attr) != value
        }

        if changed:
            self.data = replace(self.data, **changed)
            if "software" in changed:
                self._update_installed_version_cache(self.data.software)
        if fresh:
            self._async_update_activity()
        return bool(changed)

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    async def async_fetch_values(
        self,
        attrs: Iterable[str],
        *,
        probe_unsupported: bool = True,
    ) -> tuple[dict[str, Any], int, int]:
        """Fetch a set of attributes concurrently.

        Returns the values that should be applied, how many calls succeeded and
        how many were actually attempted. Attributes whose call failed are
        simply absent from the result, so the previous value is kept — except
        for UNIMPLEMENTED (or INVALID_ARGUMENT repeated
        ``_INVALID_ARGUMENT_UNSUPPORTED_AFTER`` times), which clears the
        attribute to its default rather than pinning its entities to a
        pre-breakage snapshot forever.
        """
        requested = tuple(dict.fromkeys(attrs))
        values: dict[str, Any] = {}

        if probe_unsupported:
            attr_names = requested
        else:
            attr_names = tuple(a for a in requested if a not in self._unsupported_fetches)

        if not attr_names:
            return values, 0, 0

        results = await asyncio.gather(
            *(
                asyncio.wait_for(
                    getattr(self.vehicle, _FETCH_ATTRS[attr])(), timeout=_FETCH_TIMEOUT
                )
                for attr in attr_names
            ),
            return_exceptions=True,
        )

        successful = 0
        for attr, result in zip(attr_names, results, strict=True):
            if isinstance(result, (AuthError, TokenExpiredError)):
                raise ConfigEntryAuthFailed(str(result)) from result

            if self._fetch_is_unsupported(attr, result):
                if attr not in self._unsupported_fetches:
                    self._unsupported_fetches.add(attr)
                    _LOGGER.info(
                        "Fetch %s not supported for %s: %s", attr, self.vehicle.vin, result
                    )
                values[attr] = _attr_default(attr)
                continue

            if isinstance(result, BaseException):
                level = logging.DEBUG if attr in self._unsupported_fetches else logging.WARNING
                _LOGGER.log(
                    level, "Failed to fetch %s for %s: %s", attr, self.vehicle.vin, result
                )
                continue

            self._invalid_argument_counts.pop(attr, None)
            if attr in self._unsupported_fetches:
                self._unsupported_fetches.discard(attr)
                _LOGGER.info("Fetch %s is supported again for %s", attr, self.vehicle.vin)

            merged = self._merge_partial_update(attr, getattr(self.data, attr), result)
            if merged is not None:
                values[attr] = merged
            successful += 1

        return values, successful, len(attr_names)

    def _fetch_is_unsupported(self, attr: str, result: Any) -> bool:
        """Whether a failed fetch means the backend does not serve this endpoint."""
        if not isinstance(result, GRPCError):
            return False
        if result.status is GrpcStatus.UNIMPLEMENTED:
            return True
        if result.status is not GrpcStatus.INVALID_ARGUMENT:
            return False
        count = self._invalid_argument_counts.get(attr, 0) + 1
        self._invalid_argument_counts[attr] = count
        return count >= _INVALID_ARGUMENT_UNSUPPORTED_AFTER or attr in self._unsupported_fetches

    async def _async_update_data(self) -> PolestarVehicleData:
        """Poll every attribute together on the coordinator's own timer."""
        self._poll_count += 1
        generation = self.next_fetch_generation()
        values, successful, _ = await self.async_fetch_values(
            _FETCH_ATTRS,
            probe_unsupported=self._claim_unsupported_probe(),
        )
        if successful == 0:
            raise UpdateFailed("All API calls failed")
        # No notify here: with always_update=False the base class notifies
        # entities itself when the returned snapshot differs from the last one.
        self._apply_values(values, generation)
        self.async_restart_finished_streams()
        return self.data

    def _claim_unsupported_probe(self) -> bool:
        """Whether this poll should re-probe endpoints that were UNIMPLEMENTED."""
        now = monotonic()
        last = self._last_unsupported_probe
        if last is not None and now - last < _UNSUPPORTED_REPROBE_INTERVAL:
            return False
        self._last_unsupported_probe = now
        return True

    async def async_request_attrs_refresh(self, *attrs: str) -> None:
        """Refresh only the requested attributes and notify entities."""
        if not attrs:
            await self.async_request_refresh()
            return
        generation = self.next_fetch_generation()
        values, successful, _ = await self.async_fetch_values(attrs, probe_unsupported=False)
        if successful:
            self.async_apply_values(values, generation=generation)

    @staticmethod
    def _merge_partial_update(attr: str, previous: Any, result: Any) -> Any:
        """Merge backend partial updates for attrs that are not full snapshots."""
        if result is None:
            return None
        if attr == "exterior" and previous is not None:
            return result.merge(previous)
        if attr == "climate" and previous is not None:
            # GetLatestParkingClimatization can lag behind the live stream, so a
            # post-command poll may return a snapshot older than what we already
            # hold. Applying it makes the switch flip off and back on.
            new_ts = getattr(result, "reported_at", None)
            old_ts = getattr(previous, "reported_at", None)
            if new_ts is not None and old_ts is not None and new_ts < old_ts:
                return previous
        if attr == "odometer" and previous is not None:
            # odometer_km is TOTAL_INCREASING; an out-of-order snapshot would
            # otherwise read as a counter reset in HA statistics. Compare the
            # reading itself rather than its timestamp: a single skewed backend
            # clock would pin the odometer forever, a lower reading cannot.
            if result.odometer_meters < previous.odometer_meters:
                return previous
        return result

    # ------------------------------------------------------------------
    # Live streams (optional accelerator on top of polling)
    # ------------------------------------------------------------------

    @property
    def streams_enabled(self) -> bool:
        """Whether the user opted into live subscriptions."""
        entry = self.config_entry
        if entry is None:
            return DEFAULT_ENABLE_STREAMS
        return entry.options.get(CONF_ENABLE_STREAMS, DEFAULT_ENABLE_STREAMS)

    async def async_start_streams(self) -> None:
        """Start the live subscriptions the car's current state calls for."""
        self.async_restart_finished_streams()

    def _wanted_streams(self) -> tuple[str, ...]:
        """Attributes that should have a running subscription right now."""
        if not self.streams_enabled:
            return ()
        if self._active:
            return tuple(STREAM_METHODS)
        return tuple(ALWAYS_ON_STREAMS)

    def _start_stream(self, attr: str) -> None:
        method = getattr(self.vehicle, STREAM_METHODS[attr], None)
        if method is None:
            return
        self._stream_tasks[attr] = asyncio.create_task(
            self._async_run_stream(attr, method),
            name=f"polestar-{self.vehicle.vin}-{attr}-stream",
        )

    def _stream_running(self, attr: str) -> bool:
        task = self._stream_tasks.get(attr)
        return task is not None and not task.done()

    def _stop_stream(self, attr: str) -> None:
        task = self._stream_tasks.pop(attr, None)
        if task is not None and not task.done():
            task.cancel()

    def async_restart_finished_streams(self) -> None:
        """(Re)start wanted subscriptions that are not running.

        Runs after a poll proved connectivity. No staleness heuristics: a
        stream that is merely quiet is indistinguishable from a healthy idle
        one, and polling already guarantees the data stays fresh, so only
        genuinely finished tasks are restarted.
        """
        for attr in self._wanted_streams():
            if attr in self._unsupported_streams or self._stream_running(attr):
                continue
            self._start_stream(attr)

    # ------------------------------------------------------------------
    # Activity
    # ------------------------------------------------------------------

    @property
    def car_is_active(self) -> bool:
        """Whether the car is doing something worth watching closely."""
        data = self.data
        battery = data.battery
        if battery is not None and battery.charging_status == ChargingStatus.CHARGING:
            return True
        if self.climate_is_active:
            return True
        exterior = data.exterior
        if exterior is not None and (
            exterior.is_locked is False
            or exterior.any_door_open
            or _any_window_open(exterior)
            or _tailgate_open(exterior)
        ):
            return True
        availability = data.availability
        if availability is not None and availability.usage_mode in _IN_USE_MODES:
            return True
        last = self._last_command_at
        return last is not None and monotonic() - last < COMMAND_ACTIVE_WINDOW

    @property
    def active_interval(self) -> timedelta:
        """Poll interval while the car is active."""
        seconds = (
            ACTIVE_UPDATE_INTERVAL_STREAMING
            if self.streams_enabled
            else ACTIVE_UPDATE_INTERVAL_POLLING
        )
        return min(timedelta(seconds=seconds), self.idle_interval)

    @property
    def mode(self) -> str:
        """``"active"`` or ``"idle"``, as last evaluated."""
        return "active" if self._active else "idle"

    def async_note_command(self) -> None:
        """Record that a command was sent; the car counts as active for a while."""
        self._last_command_at = monotonic()
        self._async_update_activity()

    def _async_update_activity(self) -> None:
        """Re-evaluate active/idle after new data or a command.

        Switches the poll interval and starts or stops the active-only
        streams. On idle -> active the pending idle timer is pulled in, so a
        car that was just unlocked is not left waiting out a 30-minute poll.
        """
        active = self.car_is_active
        if active == self._active:
            return
        self._active = active
        self.update_interval = self.active_interval if active else self.idle_interval
        _LOGGER.debug(
            "%s is now %s, polling every %s",
            self.vehicle.vin, self.mode, self.update_interval,
        )
        if active:
            # A poll in flight has no timer pending and schedules the next one
            # from update_interval when it finishes. Otherwise reschedule now:
            # async_request_refresh would spend a full poll on it, and
            # async_set_updated_data would force last_update_success and
            # re-render every entity.
            if self._unsub_refresh is not None:
                self._schedule_refresh()
            self.async_restart_finished_streams()
        else:
            for attr in ACTIVE_ONLY_STREAMS:
                self._stop_stream(attr)

    async def _async_run_stream(
        self,
        attr: str,
        stream_factory: Callable[[], Any],
    ) -> None:
        """Run one long-lived subscription and merge updates into the snapshot."""
        consecutive_failures = 0
        while True:
            subscribed_at = monotonic()
            try:
                received = False
                async for value in stream_factory():
                    received = True
                    consecutive_failures = 0
                    merged = self._merge_partial_update(attr, getattr(self.data, attr), value)
                    if merged is not None:
                        self.async_apply_values({attr: merged})
                if received:
                    # Server closed a subscription after delivering data (normal
                    # GOAWAY behaviour) — resubscribe, but never sooner than the
                    # floor after the last subscribe, so a server that closes
                    # after every message cannot turn this into a request loop.
                    wait = STREAM_MIN_RESUBSCRIBE_INTERVAL - (monotonic() - subscribed_at)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    continue
                consecutive_failures += 1
                delay = self._stream_retry_delay(
                    attr, consecutive_failures, RuntimeError("stream closed without data")
                )
                if delay is None:
                    return
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except (AuthError, TokenExpiredError) as err:
                _LOGGER.warning(
                    "Live %s stream auth failure for %s: %s", attr, self.vehicle.vin, err
                )
                await asyncio.sleep(STREAM_RETRY_DELAY)
            except GRPCError as err:
                if err.status is GrpcStatus.UNIMPLEMENTED:
                    _LOGGER.debug(
                        "Live %s stream not supported for %s, stopping", attr, self.vehicle.vin
                    )
                    self._unsupported_streams.add(attr)
                    return
                consecutive_failures += 1
                delay = self._stream_retry_delay(attr, consecutive_failures, err)
                if delay is None:
                    return
                await asyncio.sleep(delay)
            except Exception as err:  # noqa: BLE001
                consecutive_failures += 1
                delay = self._stream_retry_delay(attr, consecutive_failures, err)
                if delay is None:
                    return
                await asyncio.sleep(delay)

    def _stream_retry_delay(self, attr: str, failures: int, err: Exception) -> float | None:
        """Return the backoff delay in seconds, or None to stop retrying."""
        if failures >= STREAM_MAX_RETRIES:
            _LOGGER.warning(
                "Live %s stream for %s failed %d times in a row, giving up — "
                "data still updates via polling",
                attr, self.vehicle.vin, failures,
            )
            return None
        return min(STREAM_RETRY_DELAY * (2 ** (failures - 1)), 600)

    async def async_shutdown(self) -> None:
        """Cancel any running stream tasks."""
        for task in self._stream_tasks.values():
            task.cancel()
        if self._stream_tasks:
            await asyncio.gather(*self._stream_tasks.values(), return_exceptions=True)
        self._stream_tasks.clear()
        self._unsupported_streams.clear()
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @staticmethod
    def _command_succeeded(response: Any) -> bool:
        """Interpret common command response types."""
        # CarLockResponse carries its own failure code alongside a generic
        # invocation status that still reads as success — the car refusing to
        # lock because a door is ajar shows up only here.
        if getattr(response, "lock_error", 0):
            return False

        invocation = getattr(response, "response", None)
        if invocation is not None:
            return invocation.status in _COMMAND_INVOCATION_SUCCESS

        response_status = getattr(response, "response_status", None)
        if response_status is not None:
            return response_status.status in _COMMAND_RESPONSE_STATUS_SUCCESS

        if isinstance(response, int):
            try:
                status = ResponseStatusCode(response)
            except ValueError:
                return False
            return status in _COMMAND_RESPONSE_STATUS_SUCCESS

        return True

    @staticmethod
    def _command_error_message(response: Any, fallback: str) -> str:
        """Build a useful error message from the command response."""
        lock_error = getattr(response, "lock_error", 0)
        if lock_error:
            return f"{fallback} (lock error {lock_error})"

        invocation = getattr(response, "response", None)
        if invocation is not None:
            if invocation.message:
                return invocation.message
            return f"{fallback} ({invocation.status.name.lower()})"

        response_status = getattr(response, "response_status", None)
        if response_status is not None:
            return f"{fallback} ({response_status.status.name.lower()})"

        if isinstance(response, int):
            try:
                status = ResponseStatusCode(response)
            except ValueError:
                return f"{fallback} (status={response})"
            return f"{fallback} ({status.name.lower()})"

        return fallback

    async def async_run_command(
        self,
        command: Callable[[], Awaitable[Any]],
        *,
        error_message: str = "Command failed",
        timeout: int = 30,
        capability: str | None = None,
    ) -> Any:
        """Run a remote command and validate its response."""
        self.async_note_command()
        try:
            response = await asyncio.wait_for(command(), timeout=timeout)
        except TimeoutError:
            raise HomeAssistantError(f"{error_message} (timed out after {timeout}s)")
        except GRPCError as err:
            if capability and err.status in (
                GrpcStatus.UNIMPLEMENTED,
                GrpcStatus.FAILED_PRECONDITION,
            ):
                self._unsupported_commands.add(capability)
            raise HomeAssistantError(self._command_error_message(err, error_message))
        if not self._command_succeeded(response):
            raise HomeAssistantError(self._command_error_message(response, error_message))
        return response

    def is_command_supported(self, capability: str) -> bool:
        """Return whether a command capability is supported by the car."""
        return capability not in self._unsupported_commands

    def _schedule_background_refresh(self, *attrs: str) -> None:
        """Kick off a background refresh for the given attributes."""
        self.async_note_command()
        entry = self.config_entry
        assert entry is not None
        label = ",".join(attrs) if attrs else "full"
        entry.async_create_background_task(
            self.hass,
            self.async_refresh_after_command(*attrs),
            name=f"polestar-{self.vehicle.vin}-{label}-refresh",
        )

    def async_refresh_exterior_after_command(self) -> None:
        """Kick off a background exterior refresh."""
        self._schedule_background_refresh("exterior")

    async def async_refresh_after_command(self, *attrs: str) -> None:
        """Refresh after a command, allowing backend state to settle first.

        Stops as soon as any of the attributes differs from what it was when
        the command was issued — the car has reported back, by this refresh or
        by a stream in the meantime. If nothing changes, the whole schedule
        runs as the fallback.
        """
        refresh_attrs = tuple(dict.fromkeys(attrs))
        before = {attr: getattr(self.data, attr) for attr in refresh_attrs}

        def _confirmed() -> bool:
            return any(getattr(self.data, attr) != value for attr, value in before.items())

        if refresh_attrs and all(self._stream_running(attr) for attr in refresh_attrs):
            delays = _STREAMED_POST_COMMAND_REFRESH_DELAYS
        else:
            delays = _POST_COMMAND_REFRESH_DELAYS
        for delay in delays:
            await asyncio.sleep(delay)
            if _confirmed():
                return
            try:
                await self.async_request_attrs_refresh(*refresh_attrs)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Delayed refresh failed after command for %s (%s): %s",
                    self.vehicle.vin,
                    ",".join(refresh_attrs) if refresh_attrs else "full",
                    err,
                )
            if _confirmed():
                return

    # ------------------------------------------------------------------
    # Climate
    # ------------------------------------------------------------------

    @property
    def climate_target_temperature(self) -> float:
        """The temperature a climate start command will use.

        Resolution order: an explicit override set in HA, then the target the
        car itself reports, then a sane default. Both the number entity and
        ``async_start_climate`` read this same property, which is what stops
        them drifting apart — the previous design cached the HA-side value
        separately and sent an invalid 0.0 whenever the user had never touched
        the slider.
        """
        if self._climate_temperature_override is not None:
            return self._climate_temperature_override
        climate = self.data.climate if self.data else None
        reported = getattr(climate, "target_temperature_celsius", None)
        if reported:
            return float(reported)
        return DEFAULT_CLIMATE_TEMPERATURE

    def set_climate_temperature_override(self, value: float | None) -> None:
        """Override the target temperature used by climate start commands."""
        self._climate_temperature_override = value

    async def async_set_climate_temperature(self, value: float) -> None:
        """Set the climate target temperature.

        While climatisation is running the new temperature is sent to the car
        straight away — the car has no separate 'set temperature' command, so
        restarting climate with the new value is how it takes effect. While it
        is off, the value is only remembered for the next start.
        """
        self.set_climate_temperature_override(value)
        if self.climate_is_active:
            await self.async_start_climate(temperature=value)

    async def async_start_climate(
        self,
        *,
        temperature: float | None = None,
        front_left_seat: HeatingIntensity | None = None,
        front_right_seat: HeatingIntensity | None = None,
        rear_left_seat: HeatingIntensity | None = None,
        rear_right_seat: HeatingIntensity | None = None,
        steering_wheel: HeatingIntensity | None = None,
    ) -> Any:
        """Start climate using resolved preferences unless values are given."""
        prefs = self.climate_preferences
        response = await self.vehicle.start_climate(
            temperature=self.climate_target_temperature if temperature is None else temperature,
            front_left_seat=prefs.front_left_seat if front_left_seat is None else front_left_seat,
            front_right_seat=(
                prefs.front_right_seat if front_right_seat is None else front_right_seat
            ),
            rear_left_seat=prefs.rear_left_seat if rear_left_seat is None else rear_left_seat,
            rear_right_seat=prefs.rear_right_seat if rear_right_seat is None else rear_right_seat,
            steering_wheel=prefs.steering_wheel if steering_wheel is None else steering_wheel,
        )
        if not self._command_succeeded(response):
            raise HomeAssistantError(
                self._command_error_message(response, "Start climate command failed")
            )
        self._schedule_background_refresh("climate")
        return response

    async def async_stop_climate(self) -> Any:
        """Stop climate and refresh state."""
        response = await self.vehicle.stop_climate()
        if not self._command_succeeded(response):
            raise HomeAssistantError(
                self._command_error_message(response, "Stop climate command failed")
            )
        self._schedule_background_refresh("climate")
        return response

    @property
    def climate_is_active(self) -> bool:
        """Whether the car reports climatisation as currently running."""
        climate = self.data.climate if self.data else None
        return bool(getattr(climate, "is_active", False))

    # ------------------------------------------------------------------
    # Charging
    # ------------------------------------------------------------------

    @property
    def current_charge_location_details(self) -> ChargeLocation | None:
        """Return the currently active saved charge location, if any."""
        if self.data is None:
            return None
        location_id = self.data.current_charge_location.get("location_id")
        if not location_id:
            return None
        for location in self.data.charge_locations:
            if location.location_id == location_id:
                return location
        return None

    @property
    def target_soc_setting_type(self) -> ChargeTargetLevelSettingType | None:
        """The car's currently active target SoC mode, or None if unknown."""
        if self.data is None or self.data.target_soc is None:
            return None
        setting_type = self.data.target_soc.setting_type
        if setting_type == ChargeTargetLevelSettingType.UNSPECIFIED:
            return None
        return setting_type

    async def async_set_target_soc(self, level: int) -> TargetSocResponse:
        """Set the target SoC level, keeping the car's current mode."""
        mode = self.target_soc_setting_type or ChargeTargetLevelSettingType.CUSTOM
        if mode != ChargeTargetLevelSettingType.CUSTOM:
            raise HomeAssistantError(
                f"Target SoC is in {mode.name.lower().replace('_', ' ')} mode, which "
                "charges to a fixed preset and ignores a specific level. Set the "
                "'Target SOC mode' select to Custom to choose a specific level."
            )
        response = await self.async_run_command(
            lambda: self.vehicle.set_target_soc(level, mode),
            error_message="Set target SOC command failed",
        )
        self._schedule_background_refresh("target_soc")
        return response

    async def async_set_target_soc_mode(
        self, mode: ChargeTargetLevelSettingType
    ) -> TargetSocResponse:
        """Explicitly switch the car's target SoC mode (daily/long_trip/custom)."""
        level = (
            self.data.target_soc.target_level if self.data and self.data.target_soc else 0
        )
        response = await self.async_run_command(
            lambda: self.vehicle.set_target_soc(level, mode),
            error_message="Set target SOC mode command failed",
        )
        self._schedule_background_refresh("target_soc")
        return response

    async def async_set_amp_limit(self, amperage: int) -> AmpLimitResponse:
        """Set the charging amperage limit."""
        response = await self.async_run_command(
            lambda: self.vehicle.set_amp_limit(amperage),
            error_message="Set amp limit command failed",
        )
        self._schedule_background_refresh("amp_limit")
        return response

    async def async_start_charging(self) -> int:
        """Start immediate charging."""
        response = await self.async_run_command(
            self.vehicle.start_charging,
            error_message="Start charging command failed",
            capability="charging",
        )
        self._schedule_background_refresh("battery")
        return response

    async def async_stop_charging(self) -> int:
        """Stop charging."""
        response = await self.async_run_command(
            self.vehicle.stop_charging,
            error_message="Stop charging command failed",
            capability="charging",
        )
        self._schedule_background_refresh("battery")
        return response

    async def async_set_charge_timer(
        self,
        *,
        start: dt_time | None = None,
        stop: dt_time | None = None,
        activated: bool | None = None,
    ) -> ChargeTimerResponse:
        """Set the global charge timer while preserving unspecified fields."""

        def _daily(t: dt_time) -> DailyTime:
            return DailyTime(
                hour=t.hour,
                minute=t.minute,
                time_zone=TimeZoneOffset(offset_minutes=local_utc_offset_minutes()),
            )

        current_timer = BatteryChargeTimer()
        if self.data and self.data.charge_timer and self.data.charge_timer.timer:
            current_timer = self.data.charge_timer.timer

        timer = BatteryChargeTimer(
            start=current_timer.start if start is None else _daily(start),
            stop=current_timer.stop if stop is None else _daily(stop),
            activated=current_timer.activated if activated is None else activated,
        )
        response = await self.vehicle.set_charge_timer(timer)
        if not self._command_succeeded(response):
            raise HomeAssistantError(
                self._command_error_message(response, "Set charge timer command failed")
            )
        self._schedule_background_refresh("charge_timer")
        return response

    async def async_clear_charge_timer(self) -> ChargeTimerResponse:
        """Disable the global charge timer."""
        return await self.async_set_charge_timer(activated=False)

    async def async_create_charge_location(
        self,
        *,
        alias: str,
        amp_limit: int = 0,
        minimum_soc: int = 0,
        optimised_charging: bool = False,
    ) -> ChargeLocation | None:
        """Create a saved charge location at the car's current location."""
        location = await self.vehicle.create_charge_location(
            alias=alias,
            amp_limit=amp_limit,
            minimum_soc=minimum_soc,
            optimised_charging=optimised_charging,
        )
        self._schedule_background_refresh("charge_locations", "current_charge_location")
        return location

    async def async_update_charge_location(
        self,
        *,
        location_id: str,
        alias: str | None = None,
        amp_limit: int | None = None,
        minimum_soc: int | None = None,
        optimised_charging: bool | None = None,
    ) -> None:
        """Update one or more charge location properties."""
        if alias is not None:
            await self.vehicle.update_charge_location_alias(location_id, alias)
        if amp_limit is not None:
            await self.vehicle.update_charge_location_amp_limit(location_id, amp_limit)
        if minimum_soc is not None:
            await self.vehicle.update_charge_location_min_soc(location_id, minimum_soc)
        if optimised_charging is not None:
            await self.vehicle.update_charge_location_optimised(location_id, optimised_charging)
        self._schedule_background_refresh("charge_locations", "current_charge_location")

    async def async_delete_charge_location(self, location_id: str) -> None:
        """Delete a saved charge location."""
        await self.vehicle.delete_charge_location(location_id)
        self._schedule_background_refresh("charge_locations", "current_charge_location")

    # ------------------------------------------------------------------
    # Body / cabin
    # ------------------------------------------------------------------

    async def async_start_precleaning(self) -> None:
        """Start cabin pre-cleaning."""
        await self.async_run_command(
            self.vehicle.start_precleaning,
            error_message="Start pre-cleaning command failed",
            capability="precleaning",
        )
        self._schedule_background_refresh("precleaning")

    async def async_stop_precleaning(self) -> None:
        """Stop cabin pre-cleaning."""
        await self.async_run_command(
            self.vehicle.stop_precleaning,
            error_message="Stop pre-cleaning command failed",
            capability="precleaning",
        )
        self._schedule_background_refresh("precleaning")

    async def async_open_windows(self) -> Any:
        """Open all windows."""
        response = await self.async_run_command(
            self.vehicle.open_windows,
            error_message="Open windows command failed",
            capability="open_windows",
        )
        self._schedule_background_refresh("exterior")
        return response

    async def async_close_windows(self) -> Any:
        """Close all windows."""
        response = await self.async_run_command(
            self.vehicle.close_windows,
            error_message="Close windows command failed",
            capability="close_windows",
        )
        self._schedule_background_refresh("exterior")
        return response

    async def async_unlock_trunk(self) -> Any:
        """Unlock the trunk."""
        response = await self.async_run_command(
            self.vehicle.unlock_trunk,
            error_message="Unlock trunk command failed",
            capability="unlock_trunk",
        )
        self._schedule_background_refresh("exterior")
        return response

    # ------------------------------------------------------------------
    # OTA
    # ------------------------------------------------------------------

    @property
    def installed_version_cache(self) -> str | None:
        """Return the best known installed OTA version."""
        return self._installed_version_cache

    def restore_installed_version_cache(self, version: str) -> None:
        """Restore the installed OTA version cache from HA state."""
        self._installed_version_cache = version

    async def async_schedule_ota(self, relative_time: int = 0) -> Scheduler | None:
        """Schedule an OTA update using the currently advertised software id."""
        scheduler = await self.vehicle.schedule_ota(
            self._require_software_id(), relative_time=relative_time
        )
        self._schedule_background_refresh("software", "ota_schedule")
        return scheduler

    async def async_install_ota_now(self) -> Scheduler | None:
        """Install the current OTA update immediately."""
        scheduler = await self.vehicle.install_ota_now(self._require_software_id())
        self._schedule_background_refresh("software", "ota_schedule")
        return scheduler

    async def async_cancel_ota(self) -> Scheduler | None:
        """Cancel any scheduled OTA update."""
        scheduler = await self.vehicle.cancel_ota(self._require_software_id())
        self._schedule_background_refresh("software", "ota_schedule")
        return scheduler

    def _require_software_id(self) -> str:
        """Return the software id from current state or raise a service-friendly error."""
        software = self.data.software if self.data else None
        if software and software.software_id:
            return software.software_id
        raise HomeAssistantError("No OTA software id is available for this vehicle")

    def _update_installed_version_cache(self, software: CarSoftwareInfo | None) -> None:
        """Track the best known installed version for OTA entity state."""
        if software is None or not software.new_sw_version:
            return
        if software.state in {
            SoftwareState.UNKNOWN,
            SoftwareState.INSTALLATION_COMPLETED,
            SoftwareState.INSTALLATION_UNKNOWN,
        }:
            self._installed_version_cache = software.new_sw_version

    # ------------------------------------------------------------------
    # Climate timers
    # ------------------------------------------------------------------

    async def async_delete_climate_timer(self, timer_id: str) -> None:
        """Delete a parking climate timer."""
        await self.vehicle.delete_climate_timer(timer_id)
        self._schedule_background_refresh("climate_timers")

    async def async_set_climate_timer(self, timer: ParkingClimateTimer) -> int:
        """Create or update a parking climate timer."""
        status = await self.vehicle.set_climate_timer(timer)
        self._schedule_background_refresh("climate_timers")
        return status

    async def async_set_climate_timer_settings(
        self, settings: ParkingClimateTimerSettings
    ) -> int:
        """Set the default climate settings for parking climate timers."""
        status = await self.vehicle.set_climate_timer_settings(settings)
        self._schedule_background_refresh("climate_timer_settings")
        return status


def _any_window_open(exterior: ExteriorStatus) -> bool:
    windows = exterior.windows
    if windows is None:
        return False
    return any(
        window is not None and window.open_status not in _CLOSED
        for window in (
            windows.front_left, windows.front_right, windows.rear_left, windows.rear_right
        )
    )


def _tailgate_open(exterior: ExteriorStatus) -> bool:
    tailgate = exterior.tailgate
    status = tailgate.status if tailgate is not None else None
    return status is not None and status.open_status not in _CLOSED


@dataclass
class PolestarRuntimeData:
    """Runtime objects kept on the config entry for its lifetime."""

    api: PolestarApi | None
    coordinators: dict[str, PolestarCoordinator]


type PolestarConfigEntry = ConfigEntry[PolestarRuntimeData]
