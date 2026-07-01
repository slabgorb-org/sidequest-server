"""RED tests — Story 158-39 — the opponent ace's stance directive (ADR-153 §4, Task 4).

The engine floor (the deterministic fallback) keeps the duel moving, but the
COMMON path is the narrator picking the opponent's maneuver. For that pick to be
*motivated* rather than improvised, ``run_dogfight_dispatch`` surfaces the seated
opponent's stance — derived from its disposition — as a narrator directive when a
dogfight seats, the same way ``npc_agency`` surfaces an NPC's agency. The narrator
then chooses the opponent's maneuver consistent with that stance.

Contract under test (TEA-defined for Dev):

* A seated dogfight against a **hostile** ace emits a narrator directive whose
  payload tells the narrator the opponent presses the attack (aggressive stance).
* A seated dogfight against a **friendly/disengaging** pilot emits a directive
  describing evasion/disengagement — proving the stance is MOTIVATED by the
  opponent's disposition, not a constant string.

RED shape: ``run_dogfight_dispatch`` emits no stance directive today. Dev (158-39
GREEN) adds the directive on a successful seat (reading
``Npc.disposition.attitude()``). Behavior + directive contract only — never a
source grep (CLAUDE.md "No Source-Text Wiring Tests").
"""

from __future__ import annotations

import pytest

from sidequest.agents.subsystems.dogfight import run_dogfight_dispatch
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import Disposition
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag
from tests._helpers.fixture_packs import SWN_TEST_PACK, TEST_WORLD, load_fixture_pack

_PLAYER = "Maverick"
_OPPONENT = "Red Baron"

# Tokens that signal an aggressive vs a disengaging stance in the directive prose.
_PRESS_TOKENS = ("press", "aggress", "attack", "offensive", "hostile")
_DISENGAGE_TOKENS = ("disengage", "evasive", "evade", "break", "recover", "flee")


def _dogfight_dispatch() -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="dogfight",
        params={
            "type": "dogfight",
            "opponent": {"name": _OPPONENT, "description": "A dark strike fighter."},
        },
        idempotency_key="k-stance-1",
        confidence=0.9,
        visibility=VisibilityTag(visible_to="all"),
    )


def _snap_with_opponent(*, disposition_value: int) -> GameSnapshot:
    """A snapshot with a seeded pilot and a co-seated opponent NPC whose
    disposition drives the stance the handler must surface."""
    snap = GameSnapshot(genre=SWN_TEST_PACK)
    snap.genre_slug = SWN_TEST_PACK
    snap.world_slug = TEST_WORLD
    snap.characters = [
        Character(
            core=CreatureCore(name=_PLAYER, description="Playtest pilot.", personality="Calm."),
            backstory="A pilot.",
            char_class="Pilot",
            race="Human",
            stats={"Reflex": 10, "Intellect": 10},
        )
    ]
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name=_OPPONENT,
                description="An enemy ace.",
                personality="ruthless",
            ),
            disposition=Disposition(disposition_value),
        )
    )
    return snap


@pytest.mark.asyncio
async def test_hostile_opponent_emits_press_stance_directive() -> None:
    """A hostile ace seats → a narrator directive tells the narrator the opponent
    presses the attack, so the narrator's maneuver pick is goal-driven."""
    snap = _snap_with_opponent(disposition_value=-50)  # hostile
    pack = load_fixture_pack(SWN_TEST_PACK)

    out = await run_dogfight_dispatch(
        _dogfight_dispatch(),
        snapshot=snap,
        pack=pack,
        player_name=_PLAYER,
        npcs_present=[],
    )

    payloads = [d.payload.lower() for d in out.directives]
    assert any(any(tok in p for tok in _PRESS_TOKENS) for p in payloads), (
        "no aggressive-stance directive for a hostile ace — the narrator's "
        f"maneuver pick would be unmotivated. directives={payloads}"
    )


@pytest.mark.asyncio
async def test_friendly_opponent_emits_disengage_stance_directive() -> None:
    """A friendly/disengaging pilot seats → the directive describes evasion, not
    aggression. Proves the stance is MOTIVATED by disposition (not a constant):
    the same seat with the opposite attitude yields the opposite tendency."""
    snap = _snap_with_opponent(disposition_value=50)  # friendly
    pack = load_fixture_pack(SWN_TEST_PACK)

    out = await run_dogfight_dispatch(
        _dogfight_dispatch(),
        snapshot=snap,
        pack=pack,
        player_name=_PLAYER,
        npcs_present=[],
    )

    payloads = [d.payload.lower() for d in out.directives]
    assert any(any(tok in p for tok in _DISENGAGE_TOKENS) for p in payloads), (
        "no disengagement-stance directive for a friendly pilot — the stance is "
        f"not motivated by disposition. directives={payloads}"
    )


@pytest.mark.asyncio
async def test_stance_reflects_seated_opponent_when_router_did_not_name_it() -> None:
    """F1 regression (review round-trip 1): when the router does NOT name the
    opponent in ``params`` but a co-located NPC is seated as the Other (so the
    pre-seat ``threat_name`` is empty), the stance must reflect the SEATED
    opponent's disposition — not silently default to a constant "hostile".

    Here a FRIENDLY opponent is seated via ``npcs_present`` with no ``opponent``
    param. The old code looked up by the empty ``threat_name`` → missed →
    "hostile" (aggressive directive). The fix resolves the opponent from the
    seated ``encounter.actors`` (side="opponent"), so the directive describes
    disengagement. Guards against the silent pre-seat-name lookup.
    """
    from sidequest.agents.orchestrator import NpcMention

    snap = _snap_with_opponent(disposition_value=50)  # friendly "Red Baron" in snapshot.npcs
    pack = load_fixture_pack(SWN_TEST_PACK)

    # Router names NO opponent in params → threat_name == "" (the F1 trigger). A
    # co-located NPC rides npcs_present and is seated as the blue/opponent actor.
    dispatch = SubsystemDispatch(
        subsystem="dogfight",
        params={"type": "dogfight"},
        idempotency_key="k-stance-f1",
        confidence=0.9,
        visibility=VisibilityTag(visible_to="all"),
    )
    out = await run_dogfight_dispatch(
        dispatch,
        snapshot=snap,
        pack=pack,
        player_name=_PLAYER,
        npcs_present=[NpcMention(name=_OPPONENT, role="hostile", side="opponent")],
    )

    payloads = [d.payload.lower() for d in out.directives]
    assert any(any(tok in p for tok in _DISENGAGE_TOKENS) for p in payloads), (
        "stance did not reflect the seated friendly opponent — the directive was "
        f"not motivated by the SEATED disposition. directives={payloads}"
    )
    assert not any("hostile" in p for p in payloads), (
        "stance silently defaulted to 'hostile' despite a seated FRIENDLY opponent "
        f"(F1: pre-seat threat_name lookup missed). directives={payloads}"
    )
