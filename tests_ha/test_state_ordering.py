"""A command must not look like it was silently undone.

Two separate mechanisms conspire to produce that symptom, and both are
regression-tested here:

* the snapshot is written by scheduled polls, post-command refreshes and
  streams concurrently, so a slow fetch can finish after a newer one and
  overwrite it;
* the car takes seconds to minutes to report a command back, so an entity that
  stops showing its optimistic value too early falls back to the pre-command
  snapshot in the meantime.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_LOCK,
    SERVICE_TURN_ON,
    SERVICE_UNLOCK,
)
from homeassistant.core import HomeAssistant

from polestar_api.models.exterior import CentralLockStatus, ExteriorStatus, LockStatus

PREFIX = "polestar_4_test123"

LOCKED = ExteriorStatus(central_lock=CentralLockStatus(lock_status=LockStatus.LOCKED))
UNLOCKED = ExteriorStatus(central_lock=CentralLockStatus(lock_status=LockStatus.UNLOCKED))


async def _call(hass: HomeAssistant, domain: str, service: str, entity: str, **extra):
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity, **extra}, blocking=True
    )


# ---------------------------------------------------------------------------
# Ordering: newest fetch wins, not last to finish
# ---------------------------------------------------------------------------


async def test_apply_values_ignores_a_superseded_generation(coordinator, climate_on) -> None:
    """A result from an older fetch never overwrites a newer one's."""
    newer = coordinator.next_fetch_generation()
    older = coordinator.next_fetch_generation()
    # Deliberately applied out of order: `older` was claimed second here, so
    # give the *newer* label to the higher number and apply the low one last.
    coordinator.async_apply_values({"climate": climate_on}, generation=older)
    assert coordinator.data.climate.is_active

    coordinator.async_apply_values({"climate": None}, generation=newer)
    assert coordinator.data.climate.is_active, "a stale generation overwrote fresh state"


async def test_apply_values_tracks_generations_per_attribute(
    coordinator, climate_on, battery
) -> None:
    """A stale batch for one attribute must not block an unrelated fresh one."""
    first = coordinator.next_fetch_generation()
    second = coordinator.next_fetch_generation()

    coordinator.async_apply_values({"climate": climate_on}, generation=second)
    coordinator.async_apply_values({"battery": battery}, generation=first)

    assert coordinator.data.climate.is_active
    assert coordinator.data.battery is battery


async def test_slow_poll_cannot_revert_a_newer_refresh(
    hass: HomeAssistant, coordinator, mock_vehicle, climate_off, climate_on
) -> None:
    """The real race: a fetch that started first but finished last.

    Without generation ordering the slow fetch's pre-command snapshot lands on
    top of the fresh one and the entity flips back.
    """
    release_slow = asyncio.Event()
    started: list[int] = []

    async def get_climate():
        index = len(started)
        started.append(index)
        if index == 0:
            await release_slow.wait()
            return climate_off
        return climate_on

    mock_vehicle.get_climate = AsyncMock(side_effect=get_climate)

    slow = asyncio.create_task(coordinator.async_request_attrs_refresh("climate"))
    await asyncio.sleep(0)  # let the slow fetch claim its generation and start
    await coordinator.async_request_attrs_refresh("climate")
    assert coordinator.data.climate.is_active

    release_slow.set()
    await slow
    assert coordinator.data.climate.is_active, "a slow poll reverted a newer result"


# ---------------------------------------------------------------------------
# Optimistic state survives until the car actually reports back
# ---------------------------------------------------------------------------


def test_optimistic_hold_outlasts_the_post_command_refresh_window() -> None:
    """The regression that made a command look undone was a timing mismatch.

    Measured against a real car, a climatisation start took over a minute to be
    reported back. If the optimistic value expires before the last post-command
    refresh has run, the entity necessarily falls back to pre-command state.
    """
    from custom_components.polestar.coordinator import _POST_COMMAND_REFRESH_DELAYS
    from custom_components.polestar.lock import PolestarLock
    from custom_components.polestar.switch import PolestarSwitch

    horizon = sum(_POST_COMMAND_REFRESH_DELAYS)
    assert horizon >= 120, "the refresh window is shorter than the car takes to answer"
    assert PolestarSwitch._OPTIMISTIC_TTL >= horizon
    # The lock answers far quicker, and its optimistic hold deliberately does
    # not span the whole window — an automatic re-lock has to be able to show.
    assert 30 <= PolestarLock._OPTIMISTIC_TTL < horizon


async def test_climate_switch_holds_on_while_the_car_still_reports_idle(
    hass: HomeAssistant, setup_integration, coordinator, climate_off
) -> None:
    """Measured: a climatisation start can take over a minute to be reported."""
    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, f"switch.{PREFIX}_climate")
    assert hass.states.get(f"switch.{PREFIX}_climate").state == "on"

    # A poll lands while the car has not started climatising yet.
    coordinator.async_apply_values({"climate": climate_off})
    await hass.async_block_till_done()

    assert hass.states.get(f"switch.{PREFIX}_climate").state == "on", (
        "the switch fell back to the pre-command snapshot"
    )


async def test_lock_shows_the_command_before_the_car_confirms_it(
    hass: HomeAssistant, setup_integration, coordinator
) -> None:
    """The lock entity used to read the snapshot raw, with no optimistic hold."""
    coordinator.async_apply_values({"exterior": UNLOCKED})
    await hass.async_block_till_done()
    assert hass.states.get(f"lock.{PREFIX}_lock").state == "unlocked"

    await _call(hass, LOCK_DOMAIN, SERVICE_LOCK, f"lock.{PREFIX}_lock")
    assert hass.states.get(f"lock.{PREFIX}_lock").state == "locked"

    # A poll that still carries the pre-command state must not undo it.
    coordinator.async_apply_values({"exterior": UNLOCKED})
    await hass.async_block_till_done()
    assert hass.states.get(f"lock.{PREFIX}_lock").state == "locked"


async def test_lock_releases_its_optimistic_value_once_the_car_agrees(
    hass: HomeAssistant, setup_integration, coordinator
) -> None:
    """Holding the optimistic value must not hide what the car does next.

    Unlocking really is followed by an automatic re-lock a few seconds later
    when no door is opened, and that has to show.
    """
    coordinator.async_apply_values({"exterior": LOCKED})
    await hass.async_block_till_done()

    await _call(hass, LOCK_DOMAIN, SERVICE_UNLOCK, f"lock.{PREFIX}_lock")
    assert hass.states.get(f"lock.{PREFIX}_lock").state == "unlocked"

    coordinator.async_apply_values({"exterior": UNLOCKED})
    await hass.async_block_till_done()
    assert hass.states.get(f"lock.{PREFIX}_lock").state == "unlocked"

    coordinator.async_apply_values({"exterior": LOCKED})
    await hass.async_block_till_done()
    assert hass.states.get(f"lock.{PREFIX}_lock").state == "locked"


# ---------------------------------------------------------------------------
# Command failures the generic status hides
# ---------------------------------------------------------------------------


def test_lock_error_is_a_failure_even_when_the_invocation_says_sent(coordinator) -> None:
    """CarLockResponse.lock_error is where 'a door is ajar' actually shows up."""
    from polestar_api.models.invocation import InvocationResponse, InvocationStatus
    from polestar_api.models.locks import CarLockResponse

    ok = CarLockResponse(
        response=InvocationResponse(status=InvocationStatus.SENT), lock_error=0
    )
    refused = CarLockResponse(
        response=InvocationResponse(status=InvocationStatus.SENT), lock_error=3
    )

    assert coordinator._command_succeeded(ok)
    assert not coordinator._command_succeeded(refused)
    assert "3" in coordinator._command_error_message(refused, "Lock command failed")
