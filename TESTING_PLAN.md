# HA integration test coverage — status

Read `ARCHITECTURE_NOTES.md` first; it records the coordinator design these
tests cover.

**Why this exists:** several fixes shipped to `custom_components/polestar/`
with zero automated coverage, because the HA layer had never been importable
under pytest. Each "fix" surfaced the next bug in the owner's production HA.
That was a missing-harness problem, not a code-quality one.

## Where things stand

| Phase | Status | Notes |
|---|---|---|
| 0 — Infra | **done** | `tests_ha/`, `--extra ha`, `requires-python >= 3.13.2` |
| 1 — Config flow | **done** | 10 tests, `tests_ha/test_config_flow.py` |
| 2 — Coordinator | **done** | 17 tests, rewritten for the tiered design |
| 3 — Entity commands | **partial** | Climate fully covered; other command entities pending |
| 4 — CI | **done** | `.github/workflows/test.yml` |

`uv run pytest -q` → 131 passed (77 library + 54 HA), 31 live tests deselected.

## Phase 0 — Infra (done)

The blocker was real: `pytest-homeassistant-custom-component>=0.13.300` pins an
`homeassistant` that requires Python >= 3.13.2, and `requires-python = ">=3.12"`
made the whole range unsatisfiable.

Resolved by **bumping `requires-python` to `>=3.13.2`**. Justification: HA core
itself moved to 3.13.2+, so the HACS side was already effectively gated there,
and `src/polestar_api` has no known 3.12-only consumer. Resolves to HA core
2026.8.2 / p-h-c-c 0.13.356.

Layout: `tests_ha/` alongside `tests/`, both in `testpaths`. Mocking is at the
`polestar_api.vehicle.Vehicle` facade (the `tesla_fleet` pattern), not the gRPC
wire — wire parsing is already `tests/`'s job.

## Phase 1 — Config flow (done)

`tests_ha/test_config_flow.py`: happy path, `invalid_auth`, `cannot_connect`,
error recovery without restarting the flow, guest/manual-VIN fallback, demo
mode, duplicate-VIN abort, reauth success and reauth failure, options flow.

## Phase 2 — Coordinator (done)

`tests_ha/test_coordinator.py`. Rewritten against the tiered design; the
stream-specific tests the original plan listed (staleness watchdog, stagger)
were dropped because those subsystems no longer exist.

Regression guards, each tied to a bug that actually happened:

- **Odometer class** — `test_every_fetchable_attr_is_polled_by_exactly_one_tier`
  makes the "every attribute has a poll source" check permanent, plus
  `test_streamable_attrs_are_also_polled` so a stream can never be an
  attribute's only source again.
- **UNIMPLEMENTED** — clears to default, is then skipped, and is re-probed.
- **Odometer monotonicity** — an out-of-order lower reading is ignored.
- **Tier timers** — `test_tiers_have_listeners_so_their_timers_run` and
  `test_fast_tier_keeps_polling_on_its_timer`. A `DataUpdateCoordinator` with
  no listeners silently never polls; this is the guard for that.
- **Error contract** — one call failing keeps the tier alive, all calls failing
  raises `UpdateFailed` for that tier only, `AuthError` starts reauth, and one
  dead tier does not black out every entity.

`tests_ha/test_streams.py` covers the optional subscriptions: off by default,
started when enabled, a pushed value reaching entity state, restart-on-finish
via the fast tier, cancellation on unload, and that polling continues
regardless.

## Phase 3 — Entity commands (partial)

`tests_ha/test_climate.py` covers both production climate bugs and is the
phase's main point:

- start never sends `0.0 °C` (parametrised over the car reporting `None` and
  `0.0`), falls back to a valid default, and uses the car's reported target;
- the number entity's displayed value and the start command's argument are
  asserted equal, which is the structural invariant, not just the symptom;
- setting the temperature while climate is off only caches, while it's on
  pushes to the car;
- a user override survives the next poll;
- seat-heating preferences still flow through after `target_temperature` was
  removed from `ClimateCommandPreferences`.

`tests_ha/test_entity_compat.py` guards entity identity: expected `unique_id`s
still present, every one VIN-prefixed, no per-platform collisions, one device,
every platform produced entities.

**Still pending:** command coverage for `lock.py`, `button.py`, `select.py`,
`time.py`, `switch.py`'s non-climate switches and `services.py` — "assert the
right coordinator method is called with the right args", enough to catch a
button that doesn't do what its label says.

## Phase 4 — CI (done)

`.github/workflows/test.yml` runs `uv sync --extra dev --extra ha` then
`uv run pytest -q` on push to main and on PRs. Live tests stay excluded via
`addopts`.

## Out of scope (per the owner)

- Anything needing a live vehicle stays in `tests/test_live_integration.py`,
  opt-in with `-m live`, never in CI, never unattended.
- No simulating the real backend's timing or rate-limit behaviour. Unit tests
  validate our logic against a mocked boundary, not the backend's behaviour.
