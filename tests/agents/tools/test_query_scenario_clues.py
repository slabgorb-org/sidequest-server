"""Tests for the query_scenario_clues tool — Phase C Task 16.

READ tool. Surfaces the scenario clue graph state:

* discovered clues with full :class:`ClueNode` fields
* discovered_count + undiscovered_count integers
* optional ``undiscovered_titles`` (list of ids) — *GM-debug flag only*;
  narrator default is ``False`` (titles hidden)

v1 perception
~~~~~~~~~~~~~
Perception enforcement is handler-side; no separate perception rule is
registered. Undiscovered clues are hidden unless ``include_undiscovered_titles``
is true, in which case only the ids surface (no description, no links).

v1 ``discovered_clues`` is session-global on :class:`ScenarioState`. When
per-PC clue-discovery lands, the perception rule will narrow to the
perspective PC's discovered set.
"""

from __future__ import annotations

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
    query_scenario_clues as _query_scenario_clues_module,  # noqa: F401
)
from sidequest.game.persistence import SqliteStore
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.scenario import ClueGraph, ClueNode

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


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
    perspective_pc: str | None = None,
) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc=perspective_pc,
        turn_number=1,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke handler directly (bypass dispatch + perception)."""
    registered = default_registry._tools["query_scenario_clues"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_query_scenario_clues_is_registered() -> None:
    assert "query_scenario_clues" in default_registry.list_names()


# ---------------------------------------------------------------------------
# No scenario active
# ---------------------------------------------------------------------------


async def test_no_scenario_state_returns_inactive_payload() -> None:
    snapshot = _build_snapshot(scenario_state=None)
    ctx = _make_ctx(_store_with(snapshot))

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["scenario_active"] is False
    assert p["discovered"] == []
    # narrator default: titles hidden
    assert p["undiscovered_titles"] is None


async def test_no_scenario_state_with_gm_flag_still_returns_empty_titles() -> None:
    snapshot = _build_snapshot(scenario_state=None)
    ctx = _make_ctx(_store_with(snapshot))

    r = await _call({"include_undiscovered_titles": True}, ctx)
    p = _payload(r)
    assert p["scenario_active"] is False
    assert p["discovered"] == []
    assert p["undiscovered_titles"] == []


# ---------------------------------------------------------------------------
# Empty clue graph
# ---------------------------------------------------------------------------


async def test_empty_clue_graph_returns_zero_counts() -> None:
    state = _scenario_state(nodes=[], discovered=set())
    snapshot = _build_snapshot(scenario_state=state)
    ctx = _make_ctx(_store_with(snapshot))

    r = await _call({}, ctx)
    p = _payload(r)
    assert p["scenario_active"] is True
    assert p["discovered"] == []
    assert p["discovered_count"] == 0
    assert p["undiscovered_count"] == 0
    assert p["undiscovered_titles"] is None


# ---------------------------------------------------------------------------
# Narrator default: hide undiscovered
# ---------------------------------------------------------------------------


async def test_narrator_default_hides_undiscovered_titles() -> None:
    nodes = [
        _node("c1", description="bloody knife"),
        _node("c2", description="torn letter"),
        _node("c3", description="muddy boots"),
    ]
    state = _scenario_state(nodes=nodes, discovered={"c1", "c2"})
    snapshot = _build_snapshot(scenario_state=state)
    ctx = _make_ctx(_store_with(snapshot))

    r = await _call({}, ctx)
    p = _payload(r)
    assert p["scenario_active"] is True
    assert p["discovered_count"] == 2
    assert p["undiscovered_count"] == 1
    # narrator default: undiscovered titles hidden
    assert p["undiscovered_titles"] is None
    ids = sorted(c["id"] for c in p["discovered"])
    assert ids == ["c1", "c2"]


# ---------------------------------------------------------------------------
# GM flag: expose undiscovered titles only
# ---------------------------------------------------------------------------


async def test_gm_flag_exposes_undiscovered_titles_only() -> None:
    nodes = [
        _node("c1", description="bloody knife"),
        _node("c2", description="torn letter"),
        _node("c3", description="muddy boots"),
    ]
    state = _scenario_state(nodes=nodes, discovered={"c2"})
    snapshot = _build_snapshot(scenario_state=state)
    ctx = _make_ctx(_store_with(snapshot))

    r = await _call({"include_undiscovered_titles": True}, ctx)
    p = _payload(r)
    assert p["discovered_count"] == 1
    assert p["undiscovered_count"] == 2
    assert sorted(p["undiscovered_titles"]) == ["c1", "c3"]
    # discovered list contains c2 only
    assert [c["id"] for c in p["discovered"]] == ["c2"]


# ---------------------------------------------------------------------------
# Discovered clue payload shape
# ---------------------------------------------------------------------------


async def test_discovered_clue_payload_includes_all_fields() -> None:
    node = _node(
        "c1",
        clue_type="testimony",
        description="butler saw a shadow",
        discovery_method="interview",
        visibility="overt",
        locations=["parlor", "study"],
        implicates=["butler-001", "maid-002"],
        requires=["c0"],
        red_herring=False,
    )
    state = _scenario_state(nodes=[node], discovered={"c1"})
    snapshot = _build_snapshot(scenario_state=state)
    ctx = _make_ctx(_store_with(snapshot))

    r = await _call({}, ctx)
    p = _payload(r)
    assert len(p["discovered"]) == 1
    clue = p["discovered"][0]
    assert clue == {
        "id": "c1",
        "type": "testimony",
        "description": "butler saw a shadow",
        "discovery_method": "interview",
        "visibility": "overt",
        "locations": ["parlor", "study"],
        "implicates": ["butler-001", "maid-002"],
        "requires": ["c0"],
        "red_herring": False,
    }


async def test_red_herring_flag_propagates() -> None:
    nodes = [
        _node("c1", red_herring=False),
        _node("c2", red_herring=True),
    ]
    state = _scenario_state(nodes=nodes, discovered={"c1", "c2"})
    snapshot = _build_snapshot(scenario_state=state)
    ctx = _make_ctx(_store_with(snapshot))

    r = await _call({}, ctx)
    p = _payload(r)
    by_id = {c["id"]: c for c in p["discovered"]}
    assert by_id["c1"]["red_herring"] is False
    assert by_id["c2"]["red_herring"] is True


# ---------------------------------------------------------------------------
# scenario_resolved flag
# ---------------------------------------------------------------------------


async def test_scenario_resolved_flag_propagates() -> None:
    state = _scenario_state(
        nodes=[_node("c1")],
        discovered={"c1"},
        resolved=True,
    )
    snapshot = _build_snapshot(scenario_state=state)
    ctx = _make_ctx(_store_with(snapshot))

    r = await _call({}, ctx)
    p = _payload(r)
    assert p["scenario_resolved"] is True


# ---------------------------------------------------------------------------
# Session-level failure
# ---------------------------------------------------------------------------


async def test_no_session_returns_fatal_error() -> None:
    store = MagicMock()
    store.load.return_value = None
    ctx = _make_ctx(store)

    r = await _call({}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


# ---------------------------------------------------------------------------
# Dispatch + OTEL
# ---------------------------------------------------------------------------


async def test_dispatch_payload_round_trip() -> None:
    nodes = [_node("c1"), _node("c2"), _node("c3")]
    state = _scenario_state(nodes=nodes, discovered={"c1"})
    snapshot = _build_snapshot(scenario_state=state)
    ctx = _make_ctx(_store_with(snapshot))

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-disp",
            name="query_scenario_clues",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["scenario_active"] is True
    assert payload["discovered_count"] == 1
    assert payload["undiscovered_count"] == 2
    assert payload["undiscovered_titles"] is None


async def test_otel_attrs_record_counts(otel_capture) -> None:
    nodes = [_node("c1"), _node("c2"), _node("c3"), _node("c4")]
    state = _scenario_state(nodes=nodes, discovered={"c1", "c3"})
    snapshot = _build_snapshot(scenario_state=state)
    ctx = _make_ctx(_store_with(snapshot))

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="query_scenario_clues",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_scenario_clues"]
    assert read_spans, f"no dispatch span; got: {[s.name for s in spans]}"
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "query_scenario_clues"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.result_status") == "ok"
    assert attrs.get("tool.clue_graph.discovered_count") == 2
    assert attrs.get("tool.clue_graph.undiscovered_count") == 2


async def test_otel_attrs_zero_when_no_scenario(otel_capture) -> None:
    snapshot = _build_snapshot(scenario_state=None)
    ctx = _make_ctx(_store_with(snapshot))

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel-empty",
            name="query_scenario_clues",
            arguments={},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    read_spans = [s for s in spans if s.name == "tool.read.query_scenario_clues"]
    assert read_spans
    attrs = dict(read_spans[-1].attributes or {})
    assert attrs.get("tool.clue_graph.discovered_count") == 0
    assert attrs.get("tool.clue_graph.undiscovered_count") == 0
