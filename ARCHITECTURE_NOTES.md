# HA integration architecture

Design record for `custom_components/polestar`. It describes the code as it is.
The four-tier polling design that an earlier version of this file described was
reverted and is gone.

## Snapshot hub

One `PolestarCoordinator` (a `DataUpdateCoordinator`) per vehicle owns:

- the `PolestarVehicleData` snapshot, one field per attribute in `_FETCH_ATTRS`;
- every remote command;
- the listener bus that all entities subscribe to.

Polls, post-command refreshes and streams all write through
`async_apply_values`. Each fetch claims a generation number before it starts,
and an attribute is only overwritten by a newer generation, so a slow poll that
finishes late cannot put pre-command state back on top of a fresher refresh.

Entities are notified only when an applied value differs from what the snapshot
already holds (the models are frozen dataclasses with `__eq__`), and the
coordinator runs with `always_update=False`. A stream replaying an unchanged
snapshot, or a poll that brings nothing new, writes no state.
`OptimisticStateMixin` therefore schedules its own state write when an
optimistic value's TTL expires; without it, a command the car never confirmed
would show forever.

Entity `unique_id`s are `f"{vin}_{key}"` and are guarded by
`tests_ha/test_entity_compat.py`.

## Adaptive poll interval

Every poll fetches all of `_FETCH_ATTRS` at once. After each applied update the
coordinator decides whether the car is active. It is active if any of these
hold:

- battery `charging_status` is `CHARGING`;
- climatisation is running (`climate_is_active`);
- the car is unlocked, or a door, window or the tailgate is open or ajar;
- `availability.usage_mode` is `CONVENIENCE`, `ACTIVE`, `DRIVING` or `ENGINE_ON`;
- a command was sent in the last 5 minutes (`COMMAND_ACTIVE_WINDOW`).

| Mode | Interval |
|---|---|
| idle | `update_interval` option, default 1800s, range 60-3600s |
| active, streams on | 600s (`ACTIVE_UPDATE_INTERVAL_STREAMING`) |
| active, streams off | 120s (`ACTIVE_UPDATE_INTERVAL_POLLING`) |

The active interval is capped at the idle interval, so an entry configured at
300s polls every 300s in both modes. The switch is made by setting
`update_interval`, which HA reads when it schedules the next refresh. When the
car turns active between polls (a stream reports an unlock, a command is sent),
the pending idle timer is rescheduled to the active interval. That uses the
base class's `_schedule_refresh()`: `async_request_refresh` would spend a full
poll on it, and `async_set_updated_data` would force `last_update_success` and
re-render every entity.

Endpoints that answer `UNIMPLEMENTED` are cleared to their default, skipped on
later polls and post-command refreshes, and re-probed once a day.

## Selective streams

Measured on a Polestar 4 (2026-10-11): the car pushes a battery value about
every 60s and it arrives within about a second. The server closes every
subscription cleanly after about 120s, and on resubscribe immediately replays
the latest snapshot. An open stream therefore costs about 720 calls a day
whether or not anything changes, and the replay is why unchanged values must
not re-render entities.

| Stream | Runs |
|---|---|
| `battery`, `exterior`, `climate` | whenever streams are enabled |
| `location` | only while the car is active; cancelled when it goes idle |

Climate is always on so that climate started from the app or a departure timer
is seen while the car is parked, which automations rely on.
| everything else | not streamed, poll only |

Resubscribes after a clean close wait at least 30s from the previous subscribe
(`STREAM_MIN_RESUBSCRIBE_INTERVAL`). A stream that fails repeatedly backs off
and gives up after `STREAM_MAX_RETRIES`; the next successful poll restarts any
wanted stream whose task has finished. Polling stays the freshness guarantee,
so a dead stream only costs latency.

## Post-command refresh

After a command the affected attributes are refreshed at 3, 5, 10, 15, 30 and
60s intervals. The values at command time are snapshotted and the schedule
stops as soon as one of them differs, whether a refresh or a stream brought the
change. If every affected attribute has a running stream, only the 10s and 60s
steps run. With no change the whole schedule runs as the fallback.

## Call budget (estimate)

Assumptions: 21 polled endpoints, 6 of them not served on the P4 (4 answer
`UNIMPLEMENTED`, the 2 charge-location reads `INVALID_ARGUMENT`), so 15
calls per poll. One stream subscription per 120s. Commands add a handful of
calls a day and are left out.

| Scenario | Polls | Streams | Total/day |
|---|---|---|---|
| Before: 15 streams + 300s poll | 288 x 15 = 4,320 | 15 x 720 = 10,800 | ~15,100 |
| Before: streams off, 300s poll | 288 x 15 = 4,320 | 0 | ~4,300 |
| Now, defaults, 3h active + 21h idle, streams on | (18 + 42) x 15 = 900 | 3 x 720 + 30 x 3 = 2,250 | ~3,150 |
| Now, defaults, 3h active + 21h idle, streams off | (90 + 42) x 15 = 1,980 | 0 | ~2,000 |
| Now, streams on, option left at 300s | 288 x 15 = 4,320 | 2,250 | ~6,600 |

The daily re-probe of unsupported endpoints adds 6. The last row is why an
entry still set to a short interval should be raised: with streams on the idle
interval is what dominates the poll cost.

## Reading `api_calls` in diagnostics

The diagnostics download has an `api_calls` block, counted in `grpc.py` for
every call attempt including retries and stream subscribes:

- `total` and `per_method` are attempts since `since` (when the library was
  loaded, so it resets on HA restart or integration reload);
- `per_day_rate` is `total` scaled to 24 hours over the elapsed time. It is
  noisy in the first hour after a restart, because setup runs a full poll and
  opens the streams at once.

Polls and streams use different methods on the same service: polls are the
`GetLatest...` / `GetLast...` paths, and the streams are `BatteryService/GetBattery`,
`.../GetExterior`, `.../GetParkingClimatization` and
`.../StreamLastKnownLocations`. Each stream count is one subscribe, so a
healthy always-on stream accumulates about 30 an hour. Per vehicle,
`poll` shows the current `mode`, `interval_seconds` and both configured
intervals, and `streams` shows each stream's policy and whether it is running.
An active-only stream that is not started while the car is idle is expected.

## Reading `unknown_fields` in diagnostics

The `unknown_fields` block lists protobuf fields the parsers received but do
not map. It is filled by `polestar_api/unknown_fields.py` from responses the
integration already fetches, so it costs no extra calls, and like `api_calls`
it resets on HA restart or integration reload. Leave the car on normal use for
a few days (drive, lock, charge), then download diagnostics.

Keys are `label:path`. The label is the parser: `battery`, `exterior`
(digital twin), `exterior_legacy`, `location`, `climate` (digital twin),
`climate_legacy` and `health`. The path is the field number inside the inner
message, dotted for nested messages: `battery:28`, `exterior:15`,
`health:46.5`, `location:5.4` (field 4 inside the compact location at 5).
Polls and streams feed the same key.

Per key: `count` is how many times the field arrived, `last` is its latest
value, `values` holds up to 10 distinct values with the UTC time each was
first seen, and `more_values` turns true once more than 10 have appeared,
which points to a measurement rather than an enum. Proto3 omits default
values, so a `0` shows up only when the backend sends it explicitly.

What is never stored:

- strings and bytes, which appear as `bytes[<length>]`, so no VIN or
  identifier can end up here;
- a nested message shaped like a `google.protobuf.Timestamp` is stored as the
  value `timestamp`, with the time itself only in `last`;
- in `location`, floats are rounded to one decimal and unknown nested
  messages are kept as `bytes[<length>]`, so no coordinate pair is recorded.

To learn what a field means, line up when a value was first seen with what the
car was doing at that time.

## Error contract

- `AuthError` / `TokenExpiredError` from any call: `ConfigEntryAuthFailed`,
  which stops polling and starts the reauth flow.
- `AuthCooldownError`: a password login was tried less than the cooldown ago.
  It is not an `AuthError`; the call fails fast without contacting Polestar,
  is treated like any other transient failure, and recovers once the cooldown
  expires.
- One call failing: logged, that attribute keeps its previous value, the poll
  still succeeds.
- Every call in a poll failing: `UpdateFailed`.
- `UNIMPLEMENTED`: the attribute is cleared and skipped until the daily
  re-probe.
- Streams: `UNIMPLEMENTED` stops that stream for the session; other errors
  back off, and auth errors wait `STREAM_RETRY_DELAY` before resubscribing.

## Climate target temperature

`climate_target_temperature` is the single source for both the number entity
and `async_start_climate`: an override set in HA, else the target the car
reports, else 21.0. Setting the temperature while climatisation runs restarts
it with the new value, since the car has no separate set-temperature command.
