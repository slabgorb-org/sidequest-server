"""Story 108-6 (RED) — the stabilize clock is engine-owned, and success heals to 1 HP.

The narrator must not be able to fudge the death clock (the lie-detector
principle). ``rounds_elapsed`` is DERIVED from the window's ``created_turn``
provenance and the current interaction round, NOT trusted from the narrator:

  rounds_elapsed = current_round − created_turn      difficulty = 8 + rounds_elapsed

The narrator-supplied ``rounds_elapsed`` is kept only as a cross-check: if it
disagrees with the engine-derived value the tool FAILS LOUD (No Silent
Fallbacks) rather than resolving on a fudged clock.

On success the PC recovers at exactly 1 HP + Frail — today the tool clears the
Mortal Injury and appends Frail but leaves HP at 0 (a latent bug the spec
closes).

Harness mirrors test_stabilize_mortal_injury_tool.py: a duck-typed WN pack, a
real PgSaveRepository round-trip (pg_store_with), direct-handler invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tools import stabilize_mortal_injury as _stabilize_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import WwnConfig

_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}
_WINDOW_TEXT = "Mortal Injury — dies in 6 rounds unless stabilized"


@dataclass
class _WwnRules:
    ruleset: str = "wwn"
    _cfg: WwnConfig = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._cfg is None:
            self._cfg = WwnConfig(attribute_map=_AMAP)

    def ruleset_config(self) -> WwnConfig:
        return self._cfg


@dataclass
class _WwnPack:
    rules: _WwnRules = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _WwnRules()


def _pc_with_window(name: str, *, created_turn: int) -> Character:
    core = CreatureCore(
        name=name,
        description="caver",
        personality="dogged",
        inventory=Inventory(),
        hp=HpPool(current=0, max=10, base_max=10),
    )
    core.statuses.append(
        Status(
            text=_WINDOW_TEXT,
            severity=StatusSeverity.Scar,
            created_turn=created_turn,
            incapacitating=True,
            stabilizable=True,
        )
    )
    return Character(core=core, char_class="Warrior", race="Human", backstory="Born underground.")


def _snapshot(*, created_turn: int, current_turn: int) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=current_turn),
        characters=[_pc_with_window("Rux", created_turn=created_turn)],
        npcs=[],
    )


def _store(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _ctx(store) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="Rux",
        turn_number=3,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=_WwnPack(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["stabilize_mortal_injury"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


@pytest.mark.asyncio
async def test_supplied_rounds_elapsed_mismatch_fails_loud():
    # Window opened at turn 3; current is 5 → engine-derived rounds_elapsed = 2.
    # The narrator supplies 0 (a fudge that would make the check trivially easy).
    ctx = _ctx(_store(_snapshot(created_turn=3, current_turn=5)))
    with pytest.raises(ValueError, match="rounds_elapsed"):
        await _call({"actor": "Rux", "rounds_elapsed": 0, "roll": 20}, ctx)


@pytest.mark.asyncio
async def test_difficulty_uses_engine_derived_rounds():
    # Derived rounds_elapsed = 5 − 3 = 2 → difficulty = 10. A roll of 9 would
    # PASS the narrator's fudged difficulty (8) but must FAIL the real one.
    ctx = _ctx(_store(_snapshot(created_turn=3, current_turn=5)))
    result = await _call({"actor": "Rux", "rounds_elapsed": 2, "roll": 9}, ctx)
    payload = _payload(result)
    assert payload["difficulty"] == 10
    assert payload["success"] is False


@pytest.mark.asyncio
async def test_success_restores_one_hp_and_downgrades_to_frail():
    # Derived rounds_elapsed = 4 − 3 = 1 → difficulty = 9. Roll 15 succeeds.
    store = _store(_snapshot(created_turn=3, current_turn=4))
    ctx = _ctx(store)
    result = await _call({"actor": "Rux", "rounds_elapsed": 1, "roll": 15}, ctx)
    assert _payload(result)["success"] is True

    from sidequest.game.ruleset.without_number import is_dying_window_status

    core = store.load().snapshot.find_creature_core("Rux")
    assert core is not None
    assert core.hp.current == 1, "a stabilized PC recovers at exactly 1 HP"
    # Structured assertion (not a `.text` scrape — the whole point of 108-6): the
    # downgrade is a non-stabilizable Wound-severity status.
    assert any(s.severity == StatusSeverity.Wound and not s.stabilizable for s in core.statuses), (
        "Mortal Injury downgrades to a Frail Wound"
    )
    assert not any(is_dying_window_status(s) for s in core.statuses), "the window must clear"


@pytest.mark.asyncio
async def test_failed_stabilization_leaves_window_and_hp_unchanged():
    # Derived rounds_elapsed = 5 − 3 = 2 → difficulty = 10. Roll 9 FAILS.
    from sidequest.game.ruleset.without_number import is_dying_window_status

    store = _store(_snapshot(created_turn=3, current_turn=5))
    result = await _call({"actor": "Rux", "rounds_elapsed": 2, "roll": 9}, _ctx(store))
    assert _payload(result)["success"] is False

    core = store.load().snapshot.find_creature_core("Rux")
    assert core is not None
    assert core.hp.current == 0, "a failed stabilization must NOT heal"
    assert any(is_dying_window_status(s) for s in core.statuses), (
        "a failed stabilization must leave the dying window in place (timer keeps running)"
    )


@pytest.mark.asyncio
async def test_narrator_overstating_rounds_elapsed_also_fails_loud():
    # Symmetric to the under-state case: narrator OVER-states elapsed time
    # (supplies 5 when the engine derives 2). The cross-check must reject both
    # directions, not just under-statement.
    ctx = _ctx(_store(_snapshot(created_turn=3, current_turn=5)))
    with pytest.raises(ValueError, match="rounds_elapsed"):
        await _call({"actor": "Rux", "rounds_elapsed": 5, "roll": 20}, ctx)


@pytest.mark.asyncio
async def test_no_window_present_is_an_error():
    # A PC with no stabilizable window cannot be stabilized — the tool must not
    # silently invent a heal on an actor who isn't dying.
    snap = _snapshot(created_turn=3, current_turn=4)
    snap.characters[0].core.statuses = []
    result = await _call({"actor": "Rux", "rounds_elapsed": 1, "roll": 15}, _ctx(_store(snap)))
    assert result.status is not ToolResultStatus.OK, (
        "stabilizing a PC with no dying window must not return OK"
    )
