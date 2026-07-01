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
