"""RED (story 126-37): de-nativize Fate confrontation RESOLUTION — advance_confrontation guard.

The SECOND of the three downstream guards. 126-30 seated Fate standoffs with
``win_condition == "fate_conflict"`` (native dial removed). The narrator can still call
the ``advance_confrontation`` WRITE tool to nudge a metric dial — and on a Fate conflict
that dial is a vestigial placeholder the 4dF engine never reads. Left ungated, the
narrator free-hands the dead dial and pollutes the persisted forensics, exactly the
``hp_depletion`` zombie-dial bug (barsoom-2 playtest 2026-06-10).

The tool must refuse a Fate conflict the same way it already refuses ``hp_depletion``:
fail loud (recoverable — the turn proceeds on prose), leave the dial frozen, and surface
``tool.confrontation.refused_fate_conflict`` on the GM panel (No Silent Fallbacks). The
4dF engine (FATE_ACTION → fate_conflict.py) owns every Fate resolution delta.

Mirrors tests/agents/tools/test_advance_confrontation.py::
test_advance_confrontation_refuses_hp_depletion_encounter.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tools import (
    advance_confrontation as _advance_confrontation_module,  # noqa: F401 — registers the tool
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager


def _character(name: str) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(core=core, backstory="bs", char_class="Agent", race="Human")


def _encounter(*, win_condition: str, player_current: int = 0) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="standoff",
        win_condition=win_condition,
        player_metric=EncounterMetric(name="tension", current=player_current, threshold=10),
        opponent_metric=EncounterMetric(name="tension", current=1, threshold=10),
        beat=0,
    )


def _build_snapshot(*, encounter: StructuredEncounter) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="spaghetti_western",
        world_slug="coyote_star",
        turn_manager=TurnManager(interaction=1),
        characters=[_character("Reb")],
        npcs=[],
        encounter=encounter,
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(store, *, snapshot: GameSnapshot) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="Reb",
        turn_number=1,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=None,
        snapshot=snapshot,
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["advance_confrontation"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _otel_attrs(ctx: ToolContext) -> dict[str, Any]:
    span = cast(MagicMock, ctx.otel_span)
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


# ---------------------------------------------------------------------------
# 1 — RED (AC-1, AC-2): the tool must refuse a Fate-conflict dial advance.
# ---------------------------------------------------------------------------


async def test_advance_confrontation_refuses_fate_conflict_encounter() -> None:
    """AC-1/AC-2: on a ``win_condition == "fate_conflict"`` standoff the dial is a
    vestigial placeholder the 4dF engine never reads. advance_confrontation must refuse
    the same way it refuses hp_depletion: ERROR_RECOVERABLE, dial frozen,
    tool.confrontation.refused_fate_conflict on the GM panel. Today only resolved /
    hp_depletion / opposed_check are guarded, so the nudge lands and the dial drifts to
    4 — this fails (RED)."""
    enc = _encounter(win_condition="fate_conflict", player_current=0)
    snap = _build_snapshot(encounter=enc)
    ctx = _make_ctx(_store_with(snap), snapshot=snap)

    r = await _call({"axis": "player", "delta": 4}, ctx)

    assert r.status is ToolResultStatus.ERROR_RECOVERABLE, (
        "advancing a vestigial Fate-conflict dial must fail loud (recoverable) — the 4dF "
        f"engine owns Fate resolution; got status={r.status}"
    )
    assert snap.encounter is not None
    assert snap.encounter.player_metric.current == 0, (
        "a Fate-conflict dial is inert and must stay frozen — the refused nudge silently "
        f"moved it to {snap.encounter.player_metric.current}"
    )
    attrs = _otel_attrs(ctx)
    assert attrs.get("tool.confrontation.refused_fate_conflict") is True, (
        "the GM panel must see the refusal via tool.confrontation.refused_fate_conflict "
        "(sibling of refused_hp_depletion / refused_resolved)"
    )


# ---------------------------------------------------------------------------
# 2 — PIN (cross-ruleset): a native dial_threshold confrontation still advances.
# ---------------------------------------------------------------------------


async def test_advance_confrontation_allows_dial_threshold() -> None:
    """Cross-ruleset guard: the refusal MUST be ``win_condition``-gated. A native
    dial_threshold standoff still advances normally — the tool moves the dial and reports
    OK. Passes before AND after the fix (pins native behavior is untouched)."""
    enc = _encounter(win_condition="dial_threshold", player_current=0)
    snap = _build_snapshot(encounter=enc)
    ctx = _make_ctx(_store_with(snap), snapshot=snap)

    r = await _call({"axis": "player", "delta": 4}, ctx)

    assert r.status is ToolResultStatus.OK, (
        f"a native dial_threshold confrontation must advance normally; got {r.status}"
    )
    assert snap.encounter is not None
    assert snap.encounter.player_metric.current == 4, (
        f"the native dial must move by the delta; got {snap.encounter.player_metric.current}"
    )
    attrs = _otel_attrs(ctx)
    assert attrs.get("tool.confrontation.refused_fate_conflict") is not True, (
        "a native confrontation must NOT trip the Fate-conflict refusal"
    )
