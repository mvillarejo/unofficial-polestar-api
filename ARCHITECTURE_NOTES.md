# HA integration architecture

Design record for `custom_components/polestar`. Supersedes the earlier version
of this file, which posed the "should we stop streaming?" question. The answer
turned out to be "mostly, but not for the reason the question assumed."

## The finding that changed the answer

The previous note argued: HA core's `volvo` integration is platinum quality,
talks to the same vendor's cloud, and does not stream — so streaming must be
the wrong shape for this backend.

That inference does not hold up. `volvo` depends on `volvocarsapi`, and that
library is a plain aiohttp JSON client against `https://api.volvocars.com`
— Volvo's **public developer portal API**, gated by an API key. It has no
streaming transport of any kind. Its four-tier coordinator split isn't a
judgement about what the backend tolerates; the reason is written in a comment
in `volvo/__init__.py`:

```python
# Different interval coordinators are in place to keep the number
# of requests under 10000 per day.
```

It's quota budgeting against a published rate limit.

Polestar's C3 gRPC surface is a different product. It's the internal app
backend, and it exposes real server-streaming RPCs that the official mobile app
itself consumes. Streaming there is the sanctioned usage pattern, not an abuse
of a REST API that happens to hold the connection open.

So "Volvo doesn't stream" is evidence about Volvo's public REST product, not
about C3. It should not be the reason we stop streaming.

## What we do instead, and why

There is still a good reason to demote streaming, it's just a different one:
**v0.7.0 made streams load-bearing for freshness, and that is what generated
all the complexity.** Once fifteen persistent subscriptions are the mechanism
keeping entities current, every failure mode of those subscriptions becomes a
correctness problem, and you end up building a staleness watchdog, a reprobe
cycle, startup staggering, and a set of merge rules for poll-versus-stream
races. None of that was verifiable without a real car, which is why each fix
surfaced the next bug in production.

The design here inverts the relationship:

> **Tiered polling is the freshness guarantee. Streaming is an optional
> latency accelerator that is allowed to fail silently.**

That single change deletes the entire complexity mass. A dead stream no longer
means stale data, so nothing has to detect staleness. Recovery is "if the task
finished, start it again," checked on the fast tier's normal poll — no
`_stream_last_data` bookkeeping, no `_STREAM_STALE_POLL_INTERVALS`, no
stagger. And because polling alone is now sufficient, streams default to
**off**, which is the reversible choice: the owner can turn them on from the
options flow if he wants lower latency, and turning them off again costs
nothing.

## Shape: one hub, four tiers

The obvious way to copy Volvo is four `DataUpdateCoordinator` subclasses that
each own their own `data`. That doesn't work here without a large rewrite of
every platform file, because Polestar entity descriptions read across the whole
snapshot — a sensor does `c.data.battery`, its neighbour does `c.data.health`.
Splitting `data` four ways means annotating every entity with which tier owns
its field, across eleven platform modules.

So the split is between *fetching* and *holding*:

```
                    ┌──────────────────────────────────┐
                    │  PolestarCoordinator  (the hub)  │
   entities ───────▶│  • PolestarVehicleData snapshot  │
   subscribe here   │  • every remote command          │
                    │  • listener bus                  │
                    │  • update_interval = None        │
                    └──────────────▲───────────────────┘
                                   │ async_apply_values()
          ┌───────────┬────────────┼────────────┬──────────────┐
          │           │            │            │              │
     ┌────┴────┐ ┌────┴────┐ ┌─────┴────┐ ┌─────┴──────┐  ┌────┴─────┐
     │  fast   │ │ medium  │ │   slow   │ │ very_slow  │  │ streams  │
     │  1x     │ │  3x     │ │   8x     │ │   30x      │  │ optional │
     └─────────┘ └─────────┘ └──────────┘ └────────────┘  └──────────┘
              PolestarTierCoordinator × 4                  off by default
```

`PolestarCoordinator` keeps its name, its public method surface and its role as
the object entities subscribe to. It just stops polling on a timer.
`PolestarTierCoordinator` instances do the polling and push results in.

Two things fall out of this that are worth stating explicitly:

**Platform files needed essentially no changes.** Every one of them already
did `for coordinator in entry.runtime_data.coordinators.values()` and read
`coordinator.data.<attr>`. That still works, unchanged.

**`unique_id`s are byte-identical.** They're built as `f"{vehicle.vin}_{key}"`
from `coordinator.vehicle`, and every tier shares one `Vehicle`. No entity was
renamed. `tests_ha/test_entity_compat.py` asserts this rather than leaving it
to trust.

### One non-obvious requirement

A `DataUpdateCoordinator` only arms its refresh timer while it has at least one
listener. Entities subscribe to the hub, not to the tiers, so left alone the
tiers would prime once at setup and then never poll again. The hub subscribes
its own `async_update_listeners` to each tier, which both keeps the timers
running and propagates each tier's success or failure out to every entity.
`test_tiers_have_listeners_so_their_timers_run` guards it.

## Tier assignment

`TIER_MULTIPLIERS` scale everything off one configured base interval, so a user
who slows the integration down slows all of it down proportionally. Default
base is 300s (was a flat 600s for everything).

| Tier | Multiplier | Default | Endpoints |
|---|---|---|---|
| `fast` | 1× | 2 min | `battery`, `climate`, `exterior` |
| `medium` | 3× | 6 min | `location`, `parked_location`, `odometer`, `dashboard` |
| `slow` | 8× | 16 min | `health`, `availability`, `connectivity`, `precleaning`, `weather`, `target_soc`, `amp_limit` |
| `very_slow` | 30× | 60 min | `software`, `ota_schedule`, `charge_timer`, `charge_locations`, `current_charge_location`, `climate_timers`, `climate_timer_settings` |

Request budget at defaults: roughly 3,920 calls/day (2160 + 960 + 630 + 168),
against ~3,020/day for the old flat 600s poll of all 21 endpoints. About 30%
more traffic, but battery, climate and door/window state go from 10-minute to
2-minute freshness, and the
distribution is far less bursty than 21 simultaneous calls every ten minutes.
For scale, Volvo's own integration budgets against a 10,000/day ceiling.

`test_every_fetchable_attr_is_polled_by_exactly_one_tier` asserts the mapping is
total and non-overlapping. That's the permanent form of the manual check that
caught the original odometer bug, where an attribute had a stream but no poll.

## Error handling

Follows `volvo`'s `_async_update_data` contract, per tier:

- **Auth failure** (`AuthError` / `TokenExpiredError`) → `ConfigEntryAuthFailed`,
  which cancels updates and starts the reauth flow. Raised on the first
  occurrence rather than accumulated, since if one call's token is rejected
  they all will be.
- **A single call failing** → logged, that attribute keeps its previous value,
  the tier still succeeds. Losing one endpoint must not discard the others.
- **Every call in a tier failing** → `UpdateFailed` for that tier only.
- **`UNIMPLEMENTED`** → the attribute is cleared to its dataclass default (not
  left pinned to a pre-breakage snapshot), added to `_unsupported_fetches`, and
  skipped on subsequent polls, with a re-probe every `_UNSUPPORTED_REPROBE_CYCLES`
  in case the backend starts serving it again.

Entity availability is derived, not stored: the hub's `last_update_success` is
`any(tier.last_update_success)`. One dead tier doesn't black out the dashboard;
a fully offline backend does.

## Climate temperature: designed out, not patched

The production bug was that turning climate on sent `0.0 °C` whenever the user
had never moved the target-temperature slider. The proximate cause was
`ClimateCommandPreferences.target_temperature = 0.0`, an HA-side cache that
`async_start_climate` read but that nothing ever seeded from the car. The
number entity *displayed* the car's real value, so the UI looked correct while
the command was wrong — two code paths that were supposed to agree and didn't.

Adding a fallback inside `async_start_climate` would have fixed the symptom and
left the two paths free to diverge again. Instead the cache is gone. There is
one property:

```python
@property
def climate_target_temperature(self) -> float:
    if self._climate_temperature_override is not None:   # user moved the slider
        return self._climate_temperature_override
    reported = getattr(self.data.climate, "target_temperature_celsius", None)
    if reported:                                          # what the car says
        return float(reported)
    return DEFAULT_CLIMATE_TEMPERATURE                    # 21.0
```

The number entity's `value_fn` reads it. `async_start_climate` reads it. They
are the same expression, so they cannot drift.
`test_slider_and_command_read_the_same_value` states that invariant directly.

The related bug — moving the slider while climatisation was already running did
nothing — is fixed by `async_set_climate_temperature`, which restarts climate
with the new value when it's active and only records the override when it
isn't. The car has no standalone "set temperature" command, so restarting is
how a change takes effect.

## What was removed

| Removed | Why it's no longer needed |
|---|---|
| `_stream_last_data`, `_STREAM_STALE_POLL_INTERVALS` | Polling guarantees freshness; a quiet stream isn't a fault |
| `_restart_dead_streams` staleness heuristics | Replaced by "finished task → restart", checked on the fast tier |
| `_STREAM_START_STAGGER` | Four optional streams instead of fifteen mandatory ones |
| `_all_fetches_failed` | Per-tier `UpdateFailed` covers it |
| `_async_push_partial_update` | The hub has no timer to avoid deferring |
| `ClimateCommandPreferences.target_temperature` | Derived from live data instead |
| 11 of the 15 `_STREAMS` entries | Streams are an accelerator, not the mechanism |

`coordinator.py` grew from 909 to 1,114 lines, but the delta is docstrings,
section structure and the tier class — the stream lifecycle machinery it
replaced was the part that needed a real car to validate.

## Open decisions for the owner

1. **`CONF_UPDATE_INTERVAL` changed meaning.** It used to be "poll everything
   this often" (default 600s); it's now "the fast tier's interval" (default
   300s), with other tiers as multiples. An existing entry with `600` will get
   fast=600s / very_slow=5h, which is slower than before for configuration
   data. No config-entry migration was written because the values stay valid
   and the direction is conservative — but it may be worth a one-time migration
   resetting existing entries to the new 300s default so people actually get
   the freshness improvement.
2. **Streams default to off.** That's the reversible choice given the v0.7.0
   rollback. If the owner wants them on for his own install, it's one toggle in
   the options flow. If they should be on by default for everyone, flip
   `DEFAULT_ENABLE_STREAMS`.
3. **Whether to keep streams at all.** They're now genuinely optional — if the
   owner would rather carry zero streaming code, `STREAM_METHODS`,
   `_async_run_stream` and the options toggle can be deleted without touching
   anything else.

## Testing

`tests_ha/` runs under `pytest-homeassistant-custom-component`, which required
raising `requires-python` to `>=3.13.2` (HA core requires it, and the library
had no 3.12-only consumers to protect). See `TESTING_PLAN.md` for phase status.
