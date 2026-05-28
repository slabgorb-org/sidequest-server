"""Movement subsystem dispatch tests (Movement Subsystem TDD plan).

CONTENT-FREE: every test builds a synthetic ``RegionGraph`` + a minimal
fake palette + a fake ``DungeonStore`` (``feedback_no_content_coupled_tests``).
No live genre pack is loaded. Spans are captured via an in-memory OTEL
exporter monkeypatched onto ``spans.tracer`` (drive-and-assert, never a
source-text grep).
"""

from __future__ import annotations

import asyncio
import types
from dataclasses import dataclass

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems import SubsystemOutput, get_registered, run_dispatch_bank
from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# Synthetic fixtures (content-free)
# ---------------------------------------------------------------------------


def _theme(display: str = "Test Region"):
    return types.SimpleNamespace(
        display_name=display,
        narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
    )


class _FakePalette:
    """Duck-typed stand-in for ThemePalette — project_region only calls
    .get(theme_id) and reads .display_name + .narrator.{register,flavor,motifs}."""

    def get(self, theme_id: str):
        return _theme(theme_id)


@dataclass
class _FakeFrontierEdge:
    frontier_edge_id: str
    from_region_id: str
    heading: str
    spawn_depth_score: float


class _FakeStore:
    """Fake DungeonStore: serves a fixed graph and (optionally) a second
    graph after a sync-materialize call. ``frontier`` lets §Q3 fixtures
    expose an approaching edge."""

    def __init__(self, graph: RegionGraph, frontier: list[_FakeFrontierEdge] | None = None):
        self._graphs = [graph]
        self._frontier = frontier or []

    def queue_next_map(self, graph: RegionGraph) -> None:
        self._graphs.append(graph)

    def load_map(self, *, entrance_id: str) -> RegionGraph:
        # Stay on the latest map; advance only when more are queued.
        if len(self._graphs) > 1:
            return self._graphs[-1]
        return self._graphs[0]

    def load_frontier(self) -> list[_FakeFrontierEdge]:
        return list(self._frontier)


class _FakeHandle:
    """Fake LookaheadWorkerHandle exposing the one entry the handler calls
    (``_materialize_edge``) + ``persistence``/``palette`` for context
    derivation. ``materialize_effect`` is invoked to flip the store to a
    map that contains the now-committed target node."""

    def __init__(self, store: _FakeStore, palette, materialize_effect=None):
        self.persistence = store
        self.palette = palette
        self._materialize_effect = materialize_effect
        self.materialize_calls: list[str] = []

    async def _materialize_edge(self, *, edge, to_region: str, snapshot) -> None:
        self.materialize_calls.append(edge.frontier_edge_id)
        if self._materialize_effect is not None:
            self._materialize_effect()


def _snapshot(pc_regions: dict[str, str], seats: dict[str, str], **kw) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions=dict(pc_regions),
        player_seats=dict(seats),
        **kw,
    )


def _dispatch(
    direction: str = "", exit_descriptor: str = "", key: str = "mv1"
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"direction": direction, "exit_descriptor": exit_descriptor},
        idempotency_key=key,
        visibility=VisibilityTag(visible_to="all"),
    )


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-movement")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def _spans_named(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1 — deeper picks strictly-greater depth; kind tie-break.
# ---------------------------------------------------------------------------


def _graph_with(nodes, edges, entrance="entrance") -> RegionGraph:
    g = RegionGraph(entrance_id=entrance)
    for nid, depth in nodes:
        g.add_node(RegionNode(id=nid, expansion_id=0, theme="t", depth_score=depth))
    for a, b, kind, hidden in edges:
        g.add_edge(RegionEdge(a=a, b=b, kind=kind, hidden=hidden))
    return g


def test_deeper_picks_strictly_greater_depth_kind_tiebreak(capture_spans):
    # from 'a' (depth 1): two equal-delta deeper targets via different kinds
    # — shaft must win over corridor (kind tie-break shaft>chute>stairs>corridor).
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("via_corridor", 5.0), ("via_shaft", 5.0), ("up", 0.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "up", "corridor", False),
            ("a", "via_corridor", "corridor", False),
            ("a", "via_shaft", "shaft", False),
        ],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["to_region"] == "via_shaft"
    assert snap.pc_regions["Rux"] == "via_shaft"
    resolved = _spans_named(capture_spans, "movement.resolved")
    assert len(resolved) == 1
    assert resolved[0].attributes["edge_kind"] == "shaft"
    assert resolved[0].attributes["resolved_via"] == "depth_delta"


# ---------------------------------------------------------------------------
# 2 — back picks smallest-depth discovered neighbor; way-they-came tiebreak.
# ---------------------------------------------------------------------------


def test_back_picks_smallest_depth_discovered(capture_spans):
    # 'a' (depth 2) neighbors: shallow (1) + deeper (3), both discovered.
    # back goes to the smallest-depth discovered neighbor: shallow.
    g = _graph_with(
        [("a", 2.0), ("shallow", 1.0), ("deeper", 3.0)],
        [
            ("a", "shallow", "corridor", False),
            ("a", "deeper", "shaft", False),
        ],
        entrance="shallow",
    )
    store = _FakeStore(g)
    snap = _snapshot(
        {"Rux": "a"},
        {"s1": "Rux"},
        discovered_regions=["shallow", "a", "deeper"],
    )
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="back"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["to_region"] == "shallow"
    assert snap.pc_regions["Rux"] == "shallow"


def test_back_waytheycame_tiebreak(capture_spans):
    # two discovered neighbors at EQUAL smallest depth → "way they came"
    # tie-break picks the most-recently-prior in discovered_regions.
    g = _graph_with(
        [("a", 2.0), ("left", 1.0), ("right", 1.0)],
        [
            ("a", "left", "corridor", False),
            ("a", "right", "corridor", False),
        ],
        entrance="left",
    )
    store = _FakeStore(g)
    # 'right' appears LATER in discovered_regions → it is the way they came.
    snap = _snapshot(
        {"Rux": "a"},
        {"s1": "Rux"},
        discovered_regions=["left", "right", "a"],
    )
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="back"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["to_region"] == "right"


# ---------------------------------------------------------------------------
# 3 — toward_exit steps along bfs shortest path to entrance.
# ---------------------------------------------------------------------------


def test_toward_exit_bfs_step(capture_spans):
    # chain: entrance - a - b - c ; from c, toward_exit steps to b.
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("b", 2.0), ("c", 3.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "b", "corridor", False),
            ("b", "c", "corridor", False),
        ],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "c"}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="toward_exit"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["to_region"] == "b"
    resolved = _spans_named(capture_spans, "movement.resolved")
    assert resolved[0].attributes["resolved_via"] == "bfs_to_exit"


# ---------------------------------------------------------------------------
# 4 — exit_descriptor token-overlap selects the named exit; highest score.
# ---------------------------------------------------------------------------


def test_exit_descriptor_token_overlap(capture_spans):
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("iron_stair", 2.0), ("east_crack", 2.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "iron_stair", "stairs", False),
            ("a", "east_crack", "corridor", False),
        ],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(exit_descriptor="the iron stair"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["to_region"] == "iron_stair"
    resolved = _spans_named(capture_spans, "movement.resolved")
    assert resolved[0].attributes["resolved_via"] == "descriptor_match"


# ---------------------------------------------------------------------------
# 5 — hidden/secret excluded unless in discovered_routes.
# ---------------------------------------------------------------------------


def test_hidden_excluded_unless_discovered(capture_spans):
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("secret_vault", 5.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "secret_vault", "secret", True),
        ],
    )
    # not in discovered_routes → no candidate → unresolved.
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["error"] == "no_candidate_edges"
    assert snap.pc_regions["Rux"] == "a"

    # now mark the hidden edge discovered → it becomes a valid candidate.
    snap2 = _snapshot({"Rux": "a"}, {"s1": "Rux"}, discovered_routes=["secret_vault"])
    out2 = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap2,
            player_name="Rux",
            dungeon_store=_FakeStore(g),
            palette=_FakePalette(),
        )
    )
    assert out2.data["to_region"] == "secret_vault"


# ---------------------------------------------------------------------------
# 6 — resolution always in neighbors(from_region).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["deeper", "back", "toward_exit"])
def test_resolution_in_neighbors(capture_spans, direction):
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("b", 2.0), ("c", 0.5)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "b", "shaft", False),
            ("a", "c", "corridor", False),
        ],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"}, discovered_regions=["entrance", "c", "a", "b"])
    out = _run(
        run_movement_dispatch(
            _dispatch(direction=direction),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    if out.data.get("to_region"):
        assert out.data["to_region"] in g.neighbors("a")


# ---------------------------------------------------------------------------
# 8 — no candidate edges → unresolved ERROR span, no patch, directive.
# ---------------------------------------------------------------------------


def test_no_candidate_edges_fail_loud(capture_spans):
    # 'a' has only an up-edge; deeper finds nothing strictly-greater-depth.
    g = _graph_with(
        [("entrance", 0.0), ("a", 5.0)],
        [("entrance", "a", "corridor", False)],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["error"] == "no_candidate_edges"
    assert snap.pc_regions["Rux"] == "a"  # NO patch
    assert out.directives and out.directives[0].kind == "must_narrate"
    unresolved = _spans_named(capture_spans, "movement.unresolved")
    assert len(unresolved) == 1
    assert unresolved[0].attributes["reason"] == "no_candidate_edges"
    assert unresolved[0].status.status_code.name == "ERROR"
    # available_exits surfaced (the single up-edge is a real candidate).
    assert "entrance" in list(unresolved[0].attributes["available_exits"])


# ---------------------------------------------------------------------------
# 9 — ambiguous descriptor (tied top-2) → ambiguous_descriptor, no patch.
# ---------------------------------------------------------------------------


def test_ambiguous_descriptor_fail_loud(capture_spans):
    # two stairs exits — "stair" matches both with equal score → ambiguous.
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("north_stair", 2.0), ("south_stair", 2.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "north_stair", "stairs", False),
            ("a", "south_stair", "stairs", False),
        ],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(exit_descriptor="the stair"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["error"] == "ambiguous_descriptor"
    assert snap.pc_regions["Rux"] == "a"


# ---------------------------------------------------------------------------
# 10 — dungeon_store None → no_dungeon_store, no patch.
# ---------------------------------------------------------------------------


def test_no_dungeon_store_fail_loud(capture_spans):
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=None,
            palette=_FakePalette(),
        )
    )
    assert out.data["error"] == "no_dungeon_store"
    assert snap.pc_regions["Rux"] == "a"
    unresolved = _spans_named(capture_spans, "movement.unresolved")
    assert unresolved[0].attributes["reason"] == "no_dungeon_store"


# ---------------------------------------------------------------------------
# 11 — seated PC missing pc_regions entry → fail loud (no_pc_region).
# ---------------------------------------------------------------------------


def test_missing_pc_region_fail_loud(capture_spans):
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0)],
        [("entrance", "a", "corridor", False)],
    )
    store = _FakeStore(g)
    # Rux is seated but has NO pc_regions entry; current_region is set —
    # the handler must NOT read it back.
    snap = _snapshot({}, {"s1": "Rux"}, current_region="entrance")
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["error"] == "no_pc_region"
    assert "Rux" not in snap.pc_regions  # never seeded from current_region


# ---------------------------------------------------------------------------
# 12 — resolution escaping neighbors() → raises (programmer bug).
# ---------------------------------------------------------------------------


def test_resolution_escaping_neighbors_raises(capture_spans):
    # A store whose load_map returns a graph where projection (driven off
    # graph.edges) yields an exit, but neighbors() of from_region is empty —
    # we force this by giving project_region a different from_region than the
    # one neighbors() is asked about. Simulate by patching neighbors.
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("b", 2.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "b", "shaft", False),
        ],
    )

    # Corrupt neighbors() so the resolved 'b' is no longer reported adjacent.
    orig_neighbors = g.neighbors

    def _liar(region_id):
        if region_id == "a":
            return []  # pretend 'a' has no neighbors
        return orig_neighbors(region_id)

    g.neighbors = _liar  # type: ignore[method-assign]
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    with pytest.raises(RuntimeError):
        _run(
            run_movement_dispatch(
                _dispatch(direction="deeper"),
                snapshot=snap,
                player_name="Rux",
                dungeon_store=store,
                palette=_FakePalette(),
            )
        )


# ---------------------------------------------------------------------------
# 14 — path-2 ({npc_pool}-only context, no dungeon_store) → no patch.
# ---------------------------------------------------------------------------


def test_path2_no_dungeon_store_noop(capture_spans):
    # The double-dispatch path 2 supplies neither dungeon_store nor palette;
    # the handler hits the no_dungeon_store branch and applies no patch.
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
        )
    )
    assert out.data["error"] == "no_dungeon_store"
    assert snap.pc_regions["Rux"] == "a"
    assert not _spans_named(capture_spans, "frontier.region_transition")


# ---------------------------------------------------------------------------
# 15 — move into already-committed neighbor → patch applied; span attrs.
# ---------------------------------------------------------------------------


def test_move_into_committed_neighbor(capture_spans):
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("b", 2.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "b", "shaft", False),
        ],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.data["to_region"] == "b"
    assert snap.pc_regions["Rux"] == "b"
    resolved = _spans_named(capture_spans, "movement.resolved")
    assert resolved[0].attributes["target_pre_materialized"] is True
    assert resolved[0].attributes["materialize_triggered"] is True
    # The Phase-1 patch path fired a region transition for THIS pc.
    transitions = _spans_named(capture_spans, "frontier.region_transition")
    assert len(transitions) == 1
    assert transitions[0].attributes["pc_name"] == "Rux"
    assert transitions[0].attributes["to_region"] == "b"


# ---------------------------------------------------------------------------
# 16 — move toward uncommitted edge → sync materialize first, then patch.
# ---------------------------------------------------------------------------


def test_move_toward_uncommitted_edge_sync_materializes(capture_spans):
    # Initial graph: 'a' has an edge to 'frontier_target' that is NOT yet a
    # committed node (a fake store can express this invariant violation).
    g0 = RegionGraph(entrance_id="entrance")
    g0.add_node(RegionNode(id="entrance", expansion_id=0, theme="t", depth_score=0.0))
    g0.add_node(RegionNode(id="a", expansion_id=0, theme="t", depth_score=1.0))
    # bypass add_edge validation by appending the edge directly (fake an
    # uncommitted destination — exactly the §Q3 frontier case).
    g0.edges.append(RegionEdge(a="a", b="frontier_target", kind="shaft", hidden=False))
    g0.add_edge(RegionEdge(a="entrance", b="a", kind="corridor"))

    # post-materialize graph: now contains the committed frontier_target.
    g1 = RegionGraph(entrance_id="entrance")
    g1.add_node(RegionNode(id="entrance", expansion_id=0, theme="t", depth_score=0.0))
    g1.add_node(RegionNode(id="a", expansion_id=0, theme="t", depth_score=1.0))
    g1.add_node(RegionNode(id="frontier_target", expansion_id=1, theme="t", depth_score=5.0))
    g1.add_edge(RegionEdge(a="entrance", b="a", kind="corridor"))
    g1.add_edge(RegionEdge(a="a", b="frontier_target", kind="shaft"))

    frontier = [
        _FakeFrontierEdge(
            frontier_edge_id="fe1", from_region_id="a", heading="down", spawn_depth_score=2.0
        )
    ]
    store = _FakeStore(g0, frontier=frontier)
    handle = _FakeHandle(store, _FakePalette(), materialize_effect=lambda: store.queue_next_map(g1))
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    # descriptor match selects the shaft edge to the (uncommitted) target —
    # descriptor scoring does not need the target's depth_score, so it can
    # name an uncommitted frontier destination (the §Q3 case).
    out = _run(
        run_movement_dispatch(
            _dispatch(exit_descriptor="down the shaft"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
            lookahead_handle=handle,
        )
    )
    assert out.data["to_region"] == "frontier_target"
    assert handle.materialize_calls == ["fe1"]  # sync materialize ran
    assert snap.pc_regions["Rux"] == "frontier_target"
    # the resolved target is now a real node in the fresh load_map.
    assert "frontier_target" in store.load_map(entrance_id="entrance").nodes
    resolved = _spans_named(capture_spans, "movement.resolved")
    assert resolved[0].attributes["target_pre_materialized"] is False


# ---------------------------------------------------------------------------
# 21 — wiring: get_registered() includes movement → run_movement_dispatch.
# ---------------------------------------------------------------------------


def test_wiring_registry_includes_movement():
    reg = get_registered()
    assert reg.get("movement") is run_movement_dispatch


# ---------------------------------------------------------------------------
# 22 — wiring: movement dispatch flows through the bank → region advances.
# ---------------------------------------------------------------------------


def test_wiring_bank_invokes_movement(capture_spans):
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("b", 2.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "b", "shaft", False),
        ],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    package = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="Rux",
                raw_action="climb down the shaft",
                dispatch=[_dispatch(direction="deeper")],
            )
        ],
        confidence_global=0.9,
    )
    _run(
        run_dispatch_bank(
            package,
            context={
                "snapshot": snap,
                "player_name": "Rux",
                "dungeon_store": store,
                "palette": _FakePalette(),
                # extra keys other subsystems want — bank signature-filters.
                "npcs_present": [],
            },
        )
    )
    # The bank invoked run_movement_dispatch → Rux advanced.
    assert snap.pc_regions["Rux"] == "b"
    assert _spans_named(capture_spans, "movement.resolved")


# ---------------------------------------------------------------------------
# 23 — wiring: intent_router_pass threads dungeon_store+palette+player_name.
# ---------------------------------------------------------------------------


def test_wiring_intent_router_pass_threads_context(capture_spans, monkeypatch):
    from sidequest.server import intent_router_pass

    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("b", 2.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "b", "shaft", False),
        ],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})

    # Fake the router so no LLM call happens — it returns a movement package.
    class _FakeRouter:
        async def decompose(self, *, action, state_summary):
            return DispatchPackage(
                turn_id="t1",
                per_player=[
                    PlayerDispatch(
                        player_id="Rux",
                        raw_action=action,
                        dispatch=[_dispatch(direction="deeper")],
                    )
                ],
                confidence_global=0.9,
            )

    # _build_state_summary touches pack — stub it to a plain string.
    monkeypatch.setattr(
        intent_router_pass, "_build_state_summary", lambda snapshot, *, pack: "summary"
    )

    _run(
        intent_router_pass.execute_intent_router_pre_narrator_pass(
            intent_router=_FakeRouter(),
            snapshot=snap,
            pack=object(),
            action="climb down the shaft",
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    # The new context wiring reached run_movement_dispatch through the real
    # graph and moved the dispatching PC.
    assert snap.pc_regions["Rux"] == "b"
    assert _spans_named(capture_spans, "movement.resolved")


# ---------------------------------------------------------------------------
# Sanity: success returns empty directives (engine truth is the patch).
# ---------------------------------------------------------------------------


def test_success_returns_no_directives(capture_spans):
    g = _graph_with(
        [("entrance", 0.0), ("a", 1.0), ("b", 2.0)],
        [
            ("entrance", "a", "corridor", False),
            ("a", "b", "shaft", False),
        ],
    )
    store = _FakeStore(g)
    snap = _snapshot({"Rux": "a"}, {"s1": "Rux"})
    out: SubsystemOutput = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    assert out.directives == []


# ---------------------------------------------------------------------------
# Story 59-12 — surface→deep handoff: bind a surface-bound PC onto a live
# dungeon-graph node so run_movement_dispatch resolves the descent.
#
# Repro (RED): a fresh beneath_sunden PC is bound by init_region_location to
# the SURFACE cartography region 'ropefoot' (cartography.starting_region) —
# which is NOT a node of the procedural dungeon RegionGraph. The dungeon
# attach seam (session_integration) only binds the graph entrance when
# current_region is blank, and the per-turn projection treats a surface
# cartography region as the "surface lane" (returns None, no re-seed). So when
# the player descends, region_for() returns 'ropefoot', project_region() is
# called with a non-graph region, and the descent never crosses surface→deep.
#
# These tests assert the DESIGN-AGNOSTIC contract: a surface-bound PC who
# dispatches `deeper` ends up bound to a REAL dungeon-graph node, the descent
# resolves (movement.resolved fires — the OTEL lie-detector), and the move is
# mechanically backed by the per-PC WorldStatePatch path. They do NOT pin the
# target to a specific node (entrance vs first deep node) — that is the Dev /
# Architect seam decision flagged in Delivery Findings.
# ---------------------------------------------------------------------------

_SURFACE_REGION = "ropefoot"  # beneath_sunden cartography.starting_region


def _surface_to_deep_graph() -> RegionGraph:
    """A minimal procedural dungeon graph. Note: the SURFACE region
    'ropefoot' is deliberately ABSENT — it is a cartography region, never a
    graph node (the whole point of the surface→deep gap)."""
    return _graph_with(
        [("entrance", 0.0), ("deep_1", 5.0)],
        [("entrance", "deep_1", "shaft", False)],
    )


def test_surface_bound_pc_descends_onto_dungeon_graph(capture_spans):
    """AC1 — a PC bound to the surface region 'ropefoot' dispatching `deeper`
    is rebound onto a live dungeon-graph node and the descent resolves.

    RED today: region_for() returns 'ropefoot' (non-empty, passes the
    no_pc_region guard), then project_region(graph, 'ropefoot', ...) raises
    ValueError because 'ropefoot' is not a graph node — the descent crashes
    instead of crossing surface→deep.
    """
    g = _surface_to_deep_graph()
    store = _FakeStore(g)
    snap = _snapshot({"Rux": _SURFACE_REGION}, {"s1": "Rux"})
    out = _run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=store,
            palette=_FakePalette(),
        )
    )
    # The descent resolved (no honest-surface unresolved error).
    assert out.data.get("error") is None, f"descent failed: {out.data}"
    # …onto a REAL dungeon-graph node (design-agnostic: entrance OR deeper).
    assert out.data.get("to_region") in g.nodes, (
        f"resolved to non-graph node {out.data.get('to_region')!r}"
    )
    # …and the PC's per-PC region is rebound off the surface onto the graph.
    assert snap.pc_regions["Rux"] in g.nodes, (
        f"PC still stranded on non-graph region {snap.pc_regions['Rux']!r}"
    )
    resolved = _spans_named(capture_spans, "movement.resolved")
    assert len(resolved) == 1
    # …via the surface→deep handoff specifically — proves the engine took the
    # rebind path, not a coincidental in-graph resolve.
    assert resolved[0].attributes["resolved_via"] == "surface_descent"


def test_surface_descent_is_mechanically_backed_through_bank(capture_spans):
    """AC2 + AC4 — drive the descent through the REAL dispatch bank (the
    production invocation path) and assert the surface→deep crossing is
    mechanically backed, not improvised.

    The bank swallows per-handler exceptions into error spans, so the RED
    failure here is the production-observable one: the descent silently
    no-ops — no movement.resolved span fires and the PC stays stranded on the
    surface region (the Illusionism failure the GM panel must catch).

    Post-fix contract: movement.resolved fires AND a per-PC
    frontier.region_transition span proves the WorldStatePatch path advanced
    THIS PC onto a real dungeon-graph node.
    """
    g = _surface_to_deep_graph()
    store = _FakeStore(g)
    snap = _snapshot({"Rux": _SURFACE_REGION}, {"s1": "Rux"})
    package = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="Rux",
                raw_action="I climb down into the dark",
                dispatch=[_dispatch(direction="deeper")],
            )
        ],
        confidence_global=0.9,
    )
    _run(
        run_dispatch_bank(
            package,
            context={
                "snapshot": snap,
                "player_name": "Rux",
                "dungeon_store": store,
                "palette": _FakePalette(),
                "npcs_present": [],
            },
        )
    )
    # The engine resolved the descent (lie-detector: not the narrator).
    assert _spans_named(capture_spans, "movement.resolved"), (
        "no movement.resolved — descent silently no-opped (Illusionism)"
    )
    # The PC crossed surface→deep onto a real dungeon-graph node.
    assert snap.pc_regions["Rux"] in g.nodes, (
        f"PC still on surface region {snap.pc_regions['Rux']!r} after descent"
    )
    # The crossing is backed by the per-PC region-transition path.
    transitions = _spans_named(capture_spans, "frontier.region_transition")
    assert transitions, "no frontier.region_transition — descent not mechanically backed"
    last = transitions[-1]
    assert last.attributes["pc_name"] == "Rux"
    assert last.attributes["to_region"] in g.nodes
