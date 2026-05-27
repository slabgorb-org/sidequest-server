"""Tests for the advance_confrontation tool — Phase C Task 21.

WRITE tool. ADR-033 is *partial* — no formal ``Confrontation`` class
exists yet. v1 binds to :class:`StructuredEncounter`'s dual dials:
``player_metric`` and ``opponent_metric``. ``axis`` is a
``Literal["player", "opponent"]`` selector. ``confrontation_id`` is
accepted forward-compat (eventually it'll select among multiple
concurrent confrontations) but v1 always operates on
``snapshot.encounter``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import (
    advance_confrontation as _advance_confrontation_module,  # noqa: F401
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _character(name: str) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(
        core=core,
        backstory="bs",
        char_class="Delver",
        race="Human",
    )


def _encounter(
    *,
    player_current: int = 2,
    player_threshold: int = 10,
    opponent_current: int = 1,
    opponent_threshold: int = 10,
    encounter_type: str = "brawl",
) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type=encounter_type,
        player_metric=EncounterMetric(
            name="momentum",
            current=player_current,
            threshold=player_threshold,
        ),
        opponent_metric=EncounterMetric(
            name="menace",
            current=opponent_current,
            threshold=opponent_threshold,
        ),
        beat=0,
    )


def _build_snapshot(
    *,
    characters: list[Character] | None = None,
    encounter: StructuredEncounter | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
        npcs=[],
        encounter=encounter,
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(store, *, session_id: str = "s") -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=1,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["advance_confrontation"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_advance_confrontation_is_registered() -> None:
    assert "advance_confrontation" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_advance_player_axis_positive_delta() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"axis": "player", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["axis"] == "player"
    assert p["delta"] == 3
    assert p["value_before"] == 2
    assert p["value_after"] == 5
    assert p["threshold"] == 10
    assert p["crossed_threshold"] is False
    assert p["metric_name"] == "momentum"
    assert p["confrontation_id"] == ""

    # Persisted.
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.player_metric.current == 5
    # Opponent dial unchanged.
    assert reloaded.snapshot.encounter.opponent_metric.current == 1


async def test_advance_opponent_axis_negative_delta() -> None:
    """Negative deltas are permitted — the engine doesn't clamp."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=5),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"axis": "opponent", "delta": -2}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["axis"] == "opponent"
    assert p["delta"] == -2
    assert p["value_before"] == 5
    assert p["value_after"] == 3
    assert p["metric_name"] == "menace"

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.opponent_metric.current == 3


async def test_confrontation_id_default_recorded_in_otel() -> None:
    """``confrontation_id=""`` is the default and is forwarded to OTEL."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"axis": "player", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["confrontation_id"] == ""

    span = cast(MagicMock, ctx.otel_span)
    recorded = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert recorded["tool.confrontation.id"] == ""


async def test_confrontation_id_passthrough() -> None:
    """v1 ignores ``confrontation_id`` for selection but records it."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"axis": "player", "delta": 1, "confrontation_id": "future-id-123"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["confrontation_id"] == "future-id-123"

    span = cast(MagicMock, ctx.otel_span)
    recorded = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert recorded["tool.confrontation.id"] == "future-id-123"


# ---------------------------------------------------------------------------
# Threshold crossing
# ---------------------------------------------------------------------------


async def test_crossed_threshold_true_when_delta_pushes_past() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=8, player_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"axis": "player", "delta": 4}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_before"] == 8
    assert p["value_after"] == 12
    assert p["crossed_threshold"] is True

    span = cast(MagicMock, ctx.otel_span)
    recorded = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert recorded["tool.confrontation.crossed_threshold"] is True


async def test_crossed_threshold_true_exactly_at_threshold() -> None:
    """Reaching ``current == threshold`` is the trigger condition."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=7, opponent_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"axis": "opponent", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_after"] == 10
    assert p["crossed_threshold"] is True


async def test_crossed_threshold_false_when_still_below() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2, player_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"axis": "player", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["crossed_threshold"] is False


async def test_crossed_threshold_false_when_already_past() -> None:
    """If the metric was already past threshold, additional advancement
    is not a *new* crossing."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=11, player_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"axis": "player", "delta": 2}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_before"] == 11
    assert p["value_after"] == 13
    assert p["crossed_threshold"] is False


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_no_encounter_returns_fatal_error() -> None:
    snap = _build_snapshot(characters=[_character("Alice")], encounter=None)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"axis": "player", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active encounter" in r.message


async def test_no_active_session_returns_fatal_error() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    store = pg_empty_store()
    # No init_session/save → load() returns None.
    ctx = _make_ctx(store)

    r = await _call({"axis": "player", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_invalid_axis_rejected_by_args_model() -> None:
    """``Literal["player", "opponent"]`` rejects other strings; the
    validation error surfaces as a recoverable dispatch error."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-bad-axis",
            name="advance_confrontation",
            arguments={"axis": "neutral", "delta": 1},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


# ---------------------------------------------------------------------------
# OTEL attributes
# ---------------------------------------------------------------------------


async def test_otel_attrs_set_on_success() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=4, player_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {
            "confrontation_id": "fight-7",
            "axis": "player",
            "delta": 2,
            "reason": "feint succeeds",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK

    span = cast(MagicMock, ctx.otel_span)
    recorded = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert recorded["tool.confrontation.id"] == "fight-7"
    assert recorded["tool.confrontation.axis"] == "player"
    assert recorded["tool.confrontation.delta"] == 2
    assert recorded["tool.confrontation.value_after"] == 6
    assert recorded["tool.confrontation.reason"] == "feint succeeds"
    assert recorded["tool.confrontation.crossed_threshold"] is False


# ---------------------------------------------------------------------------
# Concurrency — sequential WRITE-lock
# ---------------------------------------------------------------------------


async def test_parallel_advance_against_same_session_runs_sequentially() -> None:
    """Two concurrent dispatches share the per-session WRITE lock — the
    second call must read the first's persisted value, not the initial one."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=0, player_threshold=100),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="d1",
                name="advance_confrontation",
                arguments={"axis": "player", "delta": 3},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="d2",
                name="advance_confrontation",
                arguments={"axis": "player", "delta": 5},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    # Sequential ordering: 0 → 3 → 8.
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.player_metric.current == 8

    # The two payloads form a serial sequence by value_after:
    after_values = sorted([json.loads(r.content)["value_after"] for r in results])
    assert after_values == [3, 8]
