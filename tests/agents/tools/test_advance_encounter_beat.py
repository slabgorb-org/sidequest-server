"""Tests for the advance_encounter_beat tool — Phase C Task 19.

WRITE tool. Mutates :attr:`StructuredEncounter.beat` (integer counter).

Deviation from plan
~~~~~~~~~~~~~~~~~~~
The plan's ``to_beat: str`` is a misread of the engine model — beats
are an integer counter on :class:`StructuredEncounter`, not named
identifiers. v1 uses ``to_beat: int | None``: ``None`` auto-advances
(+1), explicit sets directly. Named beats are a forward-looking design
deferred to a later story.
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
    advance_encounter_beat as _advance_encounter_beat_module,  # noqa: F401
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.persistence import SqliteStore
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


def _encounter(*, beat: int = 0, encounter_type: str = "brawl") -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type=encounter_type,
        player_metric=EncounterMetric(name="momentum", current=2, threshold=10),
        opponent_metric=EncounterMetric(name="menace", current=1, threshold=10),
        beat=beat,
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


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(store: SqliteStore, *, session_id: str = "s") -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=1,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["advance_encounter_beat"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_advance_encounter_beat_is_registered() -> None:
    assert "advance_encounter_beat" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Auto-advance (default)
# ---------------------------------------------------------------------------


async def test_auto_advance_increments_beat_by_one() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(beat=0),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["beat_from"] == 0
    assert p["beat_to"] == 1
    assert p["encounter_type"] == "brawl"
    assert p["reason"] == ""

    # Persisted.
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.beat == 1


async def test_auto_advance_from_nonzero_beat() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(beat=4),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"reason": "round resolved"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["beat_from"] == 4
    assert p["beat_to"] == 5
    assert p["reason"] == "round resolved"


# ---------------------------------------------------------------------------
# Explicit to_beat
# ---------------------------------------------------------------------------


async def test_explicit_to_beat_sets_directly() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(beat=2),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"to_beat": 5}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["beat_from"] == 2
    assert p["beat_to"] == 5

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.beat == 5


async def test_explicit_to_beat_zero_allowed() -> None:
    """``to_beat=0`` is a legitimate value (reset to start)."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(beat=3),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"to_beat": 0}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["beat_from"] == 3
    assert p["beat_to"] == 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_no_encounter_returns_fatal_error() -> None:
    snap = _build_snapshot(characters=[_character("Alice")], encounter=None)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active encounter" in r.message


async def test_no_active_session_returns_fatal_error() -> None:
    store = SqliteStore.open_in_memory()
    store.initialize()
    # No init_session/save → load() returns None.
    ctx = _make_ctx(store)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_negative_to_beat_rejected_by_args_model() -> None:
    """``ge=0`` constraint on the Pydantic args model surfaces as a validation
    error through registry dispatch."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(beat=2),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-neg",
            name="advance_encounter_beat",
            arguments={"to_beat": -1},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


# ---------------------------------------------------------------------------
# OTEL attributes
# ---------------------------------------------------------------------------


async def test_otel_attrs_on_auto_advance() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(beat=2),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"reason": "scene shifts"}, ctx)
    assert r.status is ToolResultStatus.OK

    span = cast(MagicMock, ctx.otel_span)
    recorded = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert recorded["tool.encounter.beat_from"] == 2
    assert recorded["tool.encounter.beat_to"] == 3
    assert recorded["tool.encounter.reason"] == "scene shifts"


async def test_otel_attrs_on_explicit_to_beat() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(beat=0),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"to_beat": 7, "reason": "skip ahead"}, ctx)
    assert r.status is ToolResultStatus.OK

    span = cast(MagicMock, ctx.otel_span)
    recorded = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert recorded["tool.encounter.beat_from"] == 0
    assert recorded["tool.encounter.beat_to"] == 7
    assert recorded["tool.encounter.reason"] == "skip ahead"


# ---------------------------------------------------------------------------
# Concurrency — sequential WRITE-lock
# ---------------------------------------------------------------------------


async def test_parallel_advance_against_same_session_runs_sequentially() -> None:
    """Two concurrent dispatches share the per-session WRITE lock — the
    second call must read the first's persisted beat, not the initial one."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(beat=0),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="d1",
                name="advance_encounter_beat",
                arguments={},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="d2",
                name="advance_encounter_beat",
                arguments={},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    # Sequential ordering: 0 → 1 → 2.
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.beat == 2

    # The two payloads form a serial sequence:
    payloads = sorted([json.loads(r.content)["beat_to"] for r in results])
    assert payloads == [1, 2]
