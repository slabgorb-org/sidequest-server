"""RED wiring tests — Story 158-29 (ADR-153 §7 Plan 2) — router-missed dogfight
force-dispatches through the REAL pre-narrator pass.

CLAUDE.md mandates a wiring test that drives the production path and asserts on
OTEL, never source text. This proves the full 158-29 chain end-to-end:

  1. The IntentRouter "miss" — ``decompose`` returns a package with NO
     confrontation dispatch (the 2026-06-25 coyote_star crash shape).
  2. ``execute_intent_router_pre_narrator_pass`` runs the force-dispatch injector
     (158-29 GREEN wires it in), which seats the dogfight on a strong ship-combat
     signal and emits ``dogfight.forced_dispatch``.
  3. ``run_dispatch_bank`` reaches ``run_dogfight_dispatch``, which seats the
     Plan-1 frame-default opponent and emits ``dogfight.dispatch`` — the narrator
     now has a real engine instead of a raw action it grinds to a max_turns crash.

Plus the DEGRADE-LOUD path: a dogfight that genuinely cannot seat (a frameless def
— no opponent_default_stats, no router-named contact, no co-located Other) must
reject LOUD via ``dogfight.dispatch.rejected`` and seat nothing — never wedge the
narrator and never phantom-seat. (The GENERAL "narrator max_turns must degrade,
not crash the session" robustness fix is 158-41 and is NOT asserted here — this
test only proves the DOGFIGHT path degrades observably.)

The router is a ``MagicMock`` whose ``decompose`` is an ``AsyncMock`` returning a
deterministic empty package — no LLM runs (project lore
``feedback_no_content_coupled_tests``; mirrors the proven pattern in
``tests/agents/subsystems/test_dogfight_dispatch_wiring.py::test_pre_pass_seats_dogfight_through_real_pass``).

RED until 158-29 GREEN: today the pass only logs ``confrontation_verb_unrouted``,
so no force-dispatch fires, the ``dogfight.forced_dispatch`` span never emits, and
nothing seats. Run serially (``-n0``) — OTEL span-count assertions on the shared
tracer provider (``project_server_test_otel_deadlock``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import DispatchPackage, PlayerDispatch
from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass
from tests.fixtures.dogfight_playtest_encounter import (
    DOGFIGHT_TYPE,
    GENRE_SLUG,
    WORLD_SLUG,
    make_dogfight_pack,
    make_frameless_dogfight_pack,
)

_PILOT = "Maverick"


def _empty_package(*, player_id: str = _PILOT, raw_action: str) -> DispatchPackage:
    """The router MISS: a per_player slot for the submitting seat with NO
    confrontation dispatch (decompose emitted nothing for the ship-combat verb)."""
    return DispatchPackage(
        turn_id="turn-1",
        per_player=[PlayerDispatch(player_id=player_id, raw_action=raw_action, dispatch=[])],
        confidence_global=1.0,
    )


def _snap_with_pilot(*, pilot_name: str = _PILOT) -> GameSnapshot:
    """A fresh SWN-fixture snapshot with a seeded pilot and NO active encounter —
    the whole point is that the FORCED DISPATCH seats it. Mirrors the seeding in
    ``tests/agents/subsystems/test_dogfight_dispatch_wiring.py`` (Reflex/Intellect
    at 10 for deterministic to-hit arithmetic)."""
    snap = GameSnapshot(genre=GENRE_SLUG)
    snap.genre_slug = GENRE_SLUG
    snap.world_slug = WORLD_SLUG
    snap.characters = [
        Character(
            core=CreatureCore(
                name=pilot_name,
                description="Playtest pilot.",
                personality="Calm.",
            ),
            backstory="A pilot.",
            char_class="Pilot",
            race="Human",
            stats={"Reflex": 10, "Intellect": 10},
        )
    ]
    return snap


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _dogfight_seated(snap: GameSnapshot) -> bool:
    enc = snap.encounter
    if enc is None or enc.encounter_type != DOGFIGHT_TYPE or enc.resolved:
        return False
    return sorted(a.role for a in enc.actors) == ["blue", "red"]


@pytest.mark.asyncio
async def test_router_missed_dogfight_force_dispatches_seats_and_emits_spans(
    otel_capture,
) -> None:
    """End-to-end: a strong ship-combat action the router declined to dispatch is
    force-dispatched through the REAL pass, seats a frame-default dogfight, and
    fires both ``dogfight.forced_dispatch`` and ``dogfight.dispatch``."""
    pack = make_dogfight_pack()
    snap = _snap_with_pilot()
    assert snap.encounter is None, "precondition: no encounter before the pass"

    action = "Intercept the bandit — bring guns online, lock a firing solution."
    router = MagicMock()
    router.decompose = AsyncMock(return_value=_empty_package(raw_action=action))

    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action=action,
        player_name=_PILOT,
    )

    assert _spans_named(otel_capture, "dogfight.forced_dispatch"), (
        "the pass did not force-dispatch the dogfight on a router miss — the "
        "narrator was left a raw ship-combat action (the max_turns crash shape)"
    )
    assert _dogfight_seated(snap), (
        "no live dogfight seated through the REAL pass after the forced dispatch "
        f"(encounter={snap.encounter!r}) — the engine never engaged"
    )
    assert _spans_named(otel_capture, "dogfight.dispatch"), (
        "the forced dogfight dispatch did not reach run_dogfight_dispatch "
        "(dogfight.dispatch span absent) — the bank never engaged the engine"
    )


@pytest.mark.asyncio
async def test_force_dispatch_that_cannot_seat_degrades_loud(otel_capture) -> None:
    """A frameless dogfight (no opponent_default_stats, no named/located Other)
    force-dispatches but CANNOT seat — it must reject LOUD via
    ``dogfight.dispatch.rejected`` and seat nothing, never silently fall through
    and never wrongly seat. The turn does not crash (reaching the asserts proves
    the pass returned)."""
    pack = make_frameless_dogfight_pack()
    snap = _snap_with_pilot()

    action = "Intercept and gun the bandit — lock a firing solution."
    router = MagicMock()
    router.decompose = AsyncMock(return_value=_empty_package(raw_action=action))

    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action=action,
        player_name=_PILOT,
    )

    assert _spans_named(otel_capture, "dogfight.forced_dispatch"), (
        "the router still force-dispatched (it tried to seat) — the span must fire"
    )
    assert _spans_named(otel_capture, "dogfight.dispatch.rejected"), (
        "an un-seatable forced dogfight must reject LOUD (dogfight.dispatch.rejected) "
        "— a silent fall-through is exactly the No-Silent-Fallbacks violation"
    )
    assert not _spans_named(otel_capture, "dogfight.dispatch"), (
        "a dogfight that could not seat must NOT emit the accepted dispatch span"
    )
    assert snap.encounter is None, "nothing may be seated when the dogfight cannot seat"
