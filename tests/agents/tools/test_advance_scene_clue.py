"""Tests for the advance_scene_clue tool — Phase C Task 17.

WRITE tool. Wraps :meth:`ScenarioState.discover_clue`:

* Happy path → adds clue_id to ``discovered_clues``, transition
  ``"discovered"``.
* Duplicate (clue already discovered) → still ok, transition
  ``"duplicate"``.
* Prerequisite missing → recoverable error; clue NOT added.
* Empty clue_id rejected by args validator.
* No scenario_state / no session → fatal error.
* ``evidence_text`` records to OTEL only (not persisted on
  ``ScenarioState`` — no slot for it).
* Sequential WRITE-lock works.
* A clue NOT in the clue_graph still adds to ``discovered_clues`` (per
  the :meth:`discover_clue` contract).

ADR-053.
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
from sidequest.agents.tools import advance_scene_clue as _advance_scene_clue_module  # noqa: F401
from sidequest.game.persistence import SqliteStore
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.scenario import ClueGraph, ClueNode


def _node(
    nid: str,
    *,
    description: str = "a clue",
    clue_type: str = "physical",
    discovery_method: str = "search",
    visibility: str = "hidden",
    locations: list[str] | None = None,
    implicates: list[str] | None = None,
    requires: list[str] | None = None,
    red_herring: bool = False,
) -> ClueNode:
    return ClueNode(
        id=nid,
        type=clue_type,
        description=description,
        discovery_method=discovery_method,
        visibility=visibility,
        locations=locations or [],
        implicates=implicates or [],
        requires=requires or [],
        red_herring=red_herring,
    )


def _scenario_state(
    nodes: list[ClueNode] | None = None,
    discovered: set[str] | None = None,
    *,
    resolved: bool = False,
) -> ScenarioState:
    return ScenarioState(
        clue_graph=ClueGraph(nodes=nodes or []),
        discovered_clues=discovered or set(),
        resolved=resolved,
    )


def _build_snapshot(scenario_state: ScenarioState | None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="seaboard_of_saints",
        turn_manager=TurnManager(interaction=1),
        characters=[],
        encounter=None,
        scenario_state=scenario_state,
    )


def _store_with(snapshot: GameSnapshot) -> SqliteStore:
    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=snapshot.genre_slug, world_slug=snapshot.world_slug)
    store.save(snapshot)
    return store


def _make_ctx(
    store: SqliteStore | MagicMock,
    *,
    perspective_pc: str | None = "Alice",
    session_id: str = "s",
) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc=perspective_pc,
        turn_number=1,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["advance_scene_clue"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_advance_scene_clue_is_registered() -> None:
    assert "advance_scene_clue" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_advance_discovers_clue_and_persists() -> None:
    state = _scenario_state(nodes=[_node("c1")], discovered=set())
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"clue_id": "c1"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["clue_id"] == "c1"
    assert p["transition"] == "discovered"
    assert p["discovered_count"] == 1
    assert p["perspective_pc"] == "Alice"

    # Persisted across reload.
    reloaded = store.load()
    assert reloaded is not None
    ss = reloaded.snapshot.scenario_state
    assert ss is not None
    assert "c1" in ss.discovered_clues


async def test_advance_duplicate_returns_duplicate_transition() -> None:
    state = _scenario_state(nodes=[_node("c1")], discovered={"c1"})
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"clue_id": "c1"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["transition"] == "duplicate"
    assert p["discovered_count"] == 1


async def test_advance_clue_not_in_graph_still_adds() -> None:
    """`discover_clue` contract: clues absent from the graph pass through."""
    state = _scenario_state(nodes=[], discovered=set())
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"clue_id": "off-graph"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["transition"] == "discovered"
    assert p["discovered_count"] == 1

    reloaded = store.load()
    assert reloaded is not None
    ss = reloaded.snapshot.scenario_state
    assert ss is not None
    assert "off-graph" in ss.discovered_clues


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------


async def test_prerequisite_missing_returns_recoverable_error() -> None:
    nodes = [
        _node("c0"),
        _node("c1", requires=["c0"]),
    ]
    state = _scenario_state(nodes=nodes, discovered=set())
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"clue_id": "c1"}, ctx)
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE
    assert r.message is not None
    assert "c1" in r.message
    assert "c0" in r.message

    # Not added.
    reloaded = store.load()
    assert reloaded is not None
    ss = reloaded.snapshot.scenario_state
    assert ss is not None
    assert "c1" not in ss.discovered_clues


async def test_prerequisite_satisfied_advances_normally() -> None:
    nodes = [
        _node("c0"),
        _node("c1", requires=["c0"]),
    ]
    state = _scenario_state(nodes=nodes, discovered={"c0"})
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"clue_id": "c1"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["transition"] == "discovered"
    assert p["discovered_count"] == 2


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


async def test_empty_clue_id_rejected_by_args_model() -> None:
    state = _scenario_state(nodes=[_node("c1")], discovered=set())
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-empty",
            name="advance_scene_clue",
            arguments={"clue_id": ""},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


# ---------------------------------------------------------------------------
# Session-level / scenario-level failures
# ---------------------------------------------------------------------------


async def test_no_session_returns_fatal_error() -> None:
    store = MagicMock()
    store.load.return_value = None
    ctx = _make_ctx(store)

    r = await _call({"clue_id": "c1"}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_no_scenario_state_returns_fatal_error() -> None:
    snap = _build_snapshot(scenario_state=None)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"clue_id": "c1"}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "scenario_state" in r.message


# ---------------------------------------------------------------------------
# evidence_text → OTEL only (not persisted)
# ---------------------------------------------------------------------------


async def test_evidence_text_not_persisted_on_scenario_state() -> None:
    state = _scenario_state(nodes=[_node("c1")], discovered=set())
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call(
        {"clue_id": "c1", "evidence_text": "found in the parlor desk drawer"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK

    # No slot on ScenarioState for evidence text — only the clue id lives in
    # discovered_clues.
    reloaded = store.load()
    assert reloaded is not None
    ss = reloaded.snapshot.scenario_state
    assert ss is not None
    assert ss.discovered_clues == {"c1"}


# ---------------------------------------------------------------------------
# OTEL
# ---------------------------------------------------------------------------


async def test_otel_span_carries_clue_attrs_on_success(otel_capture) -> None:
    state = _scenario_state(nodes=[_node("c1")], discovered=set())
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-disc",
            name="advance_scene_clue",
            arguments={"clue_id": "c1", "evidence_text": "in the desk"},
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.advance_scene_clue"]
    assert write_spans, f"no dispatch span; got: {[s.name for s in spans]}"
    attrs = dict(write_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "advance_scene_clue"
    assert attrs.get("tool.category") == "write"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.clue.id") == "c1"
    assert attrs.get("tool.clue.transition") == "discovered"
    assert attrs.get("tool.clue.perspective_pc") == "Alice"
    assert attrs.get("tool.clue.evidence_text") == "in the desk"


async def test_otel_span_records_duplicate_transition(otel_capture) -> None:
    state = _scenario_state(nodes=[_node("c1")], discovered={"c1"})
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-dup",
            name="advance_scene_clue",
            arguments={"clue_id": "c1"},
        ),
        ctx,
    )
    assert out.is_error is False

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.advance_scene_clue"]
    assert write_spans
    attrs = dict(write_spans[-1].attributes or {})
    assert attrs.get("tool.clue.transition") == "duplicate"


async def test_otel_span_records_blocked_transition_on_prerequisite(otel_capture) -> None:
    nodes = [_node("c0"), _node("c1", requires=["c0"])]
    state = _scenario_state(nodes=nodes, discovered=set())
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-block",
            name="advance_scene_clue",
            arguments={"clue_id": "c1"},
        ),
        ctx,
    )
    # Recoverable error → is_error True
    assert out.is_error is True

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.advance_scene_clue"]
    assert write_spans
    attrs = dict(write_spans[-1].attributes or {})
    assert attrs.get("tool.clue.id") == "c1"
    assert attrs.get("tool.clue.transition") == "blocked_by_prerequisite"
    # missing_prerequisites is a list of strings — OTEL stores list attrs as
    # tuple in the in-memory exporter.
    missing = attrs.get("tool.clue.missing_prerequisites")
    assert missing is not None
    assert list(missing) == ["c0"]


# ---------------------------------------------------------------------------
# Dispatch round-trip
# ---------------------------------------------------------------------------


async def test_dispatch_payload_round_trip() -> None:
    state = _scenario_state(nodes=[_node("c1")], discovered=set())
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store, perspective_pc="Alice")

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-disp",
            name="advance_scene_clue",
            arguments={"clue_id": "c1"},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["clue_id"] == "c1"
    assert payload["transition"] == "discovered"
    assert payload["discovered_count"] == 1
    assert payload["perspective_pc"] == "Alice"


# ---------------------------------------------------------------------------
# WRITE-lock serialization
# ---------------------------------------------------------------------------


async def test_parallel_advances_run_sequentially() -> None:
    state = _scenario_state(nodes=[_node("c1"), _node("c2")], discovered=set())
    snap = _build_snapshot(scenario_state=state)
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-scenario")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="a1",
                name="advance_scene_clue",
                arguments={"clue_id": "c1"},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="a2",
                name="advance_scene_clue",
                arguments={"clue_id": "c2"},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    reloaded = store.load()
    assert reloaded is not None
    ss = reloaded.snapshot.scenario_state
    assert ss is not None
    assert ss.discovered_clues == {"c1", "c2"}
