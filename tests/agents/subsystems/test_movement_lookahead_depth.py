"""Bug B (2026-06-22 findings): the engine ran dry one room deeper.

Live trace: a depth-delta jump landed the PC on ``exp002.r2`` (entrance ->
exp002.r2, skipping exp001), so that node's onward ring was never
materialized. The ONLY materialized neighbor of exp002.r2 was the entrance it
came from — a BACKWARD edge. So ``project_region`` yielded a non-empty exit
list (the ``if not candidates:`` guard did NOT fire), but ``_resolve`` for a
``deeper`` intent filtered that backward edge out (its depth is shallower) and
returned ``None`` -> the engine emitted ``movement.unresolved
reason=no_candidate_edges from=exp002.r2 direction=deeper`` while the narrator
improvised four exits.

The fix: on a ``deeper`` (or ``back``/``toward_exit``) intent that resolves to
nothing, if the frontier rooted at the current region has unmaterialized edges,
sync-expand that ring and re-resolve ONCE before failing loud. No Silent
Fallbacks: if expansion adds nothing, the original ``no_candidate_edges``
unresolved span still fires.

These tests reuse the seam-crossing kit's store/palette doubles
(``_StoreWithDeepGraph`` / ``_FakePalette``) and add an
``_StoreWithUnexpandedRing`` double + a ``_FakeLookaheadHandle`` whose
``_materialize_edge`` grows the store's graph the way the real worker does.
"""

from __future__ import annotations

import types

import pytest

from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.dungeon.persistence import FrontierEdge
from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from tests.agents.subsystems.test_movement_seam_crossing import (
    _FakePalette,
    _movement,
    _run,
    capture_spans,  # noqa: F401 — re-exported fixture used by these tests
)

# Current node and the (already-materialized) deep room we expect the ring
# expansion to add an edge to.
_CUR = "exp002.r2"
_DEEP = "exp003.r0"
_CUR_DEPTH = 15.0
_DEEP_DEPTH = 20.0


class _StoreWithUnexpandedRing:
    """DungeonStore double modelling the live Bug-B shape.

    ``load_map`` returns ``exp002.r2`` with its ONLY materialized neighbor
    being the entrance it jumped in from (a backward edge, shallower depth) —
    so a ``deeper`` intent has a candidate (entrance) but it resolves to None.
    ``load_frontier`` returns ONE edge rooted at ``exp002.r2`` heading deeper:
    the ring the depth-delta jump outran. ``_materialize_edge`` (driven by the
    fake handle) appends ``exp003.r0`` + a forward edge and clears the frontier,
    exactly as the real materialize path would.
    """

    def __init__(self) -> None:
        self._ring_expanded = False

    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(
            RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar", depth_score=0.0)
        )
        g.add_node(
            RegionNode(id=_CUR, expansion_id=2, theme="shaft_collar", depth_score=_CUR_DEPTH)
        )
        # The ONLY materialized edge from the current node is the backward one
        # to the entrance it jumped in from.
        g.add_edge(RegionEdge(a=entrance_id, b=_CUR, kind="shaft"))
        if self._ring_expanded:
            # After the ring expands, the deep room and its forward edge exist.
            g.add_node(
                RegionNode(id=_DEEP, expansion_id=3, theme="shaft_collar", depth_score=_DEEP_DEPTH)
            )
            g.add_edge(RegionEdge(a=_CUR, b=_DEEP, kind="shaft"))
        return g

    def load_frontier(self):
        if self._ring_expanded:
            return []
        return [
            FrontierEdge(
                frontier_edge_id="fe_deep_1",
                from_region_id=_CUR,
                heading="deeper",
                spawn_depth_score=_DEEP_DEPTH,
            )
        ]


class _StoreWithDryRing:
    """Same as ``_StoreWithUnexpandedRing`` but expansion adds NOTHING.

    The frontier rooted at the current node is empty (a genuinely dead end), so
    ring expansion cannot grow the map — the engine MUST still fail loud with
    the original ``no_candidate_edges`` (No Silent Fallbacks)."""

    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(
            RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar", depth_score=0.0)
        )
        g.add_node(
            RegionNode(id=_CUR, expansion_id=2, theme="shaft_collar", depth_score=_CUR_DEPTH)
        )
        g.add_edge(RegionEdge(a=entrance_id, b=_CUR, kind="shaft"))
        return g

    def load_frontier(self):
        return []  # nothing rooted here to expand


class _FakeLookaheadHandle:
    """Duck-typed LookaheadWorkerHandle: ``_materialize_edge`` flips the store's
    ``_ring_expanded`` flag so the next ``load_map`` shows the new ring, exactly
    as the real worker's materialize-then-persist path does."""

    def __init__(self, store) -> None:
        self._store = store
        self.calls: list[tuple[str, str]] = []

    async def _materialize_edge(self, *, edge, to_region, snapshot) -> None:
        # The worker's convention: when expanding the ring rooted at a region,
        # to_region IS that region. Record the call shape so the test can assert
        # the helper used the real keyword contract.
        self.calls.append((edge.from_region_id, to_region))
        self._store._ring_expanded = True


def _snapshot() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Groucho": _CUR},
        player_seats={"p1": "Groucho"},
    )
    snap.discovered_regions.append(ENTRANCE_ID)
    snap.discovered_regions.append(_CUR)
    return snap


class _RingKit:
    def __init__(self, snapshot, store, palette, lookahead_handle, pack) -> None:
        self.snapshot = snapshot
        self.store = store
        self.palette = palette
        self.lookahead_handle = lookahead_handle
        self.pack = pack


def _room_graph_pack():
    """Duck-typed GenrePack whose world is room_graph (NOT region-mode) so the
    procedural navigator runs end-to-end rather than deferring."""
    world = types.SimpleNamespace(cartography=None)
    return types.SimpleNamespace(worlds={"beneath_sunden": world})


@pytest.fixture
def unexpanded_ring_kit():
    store = _StoreWithUnexpandedRing()
    return _RingKit(
        _snapshot(), store, _FakePalette(), _FakeLookaheadHandle(store), _room_graph_pack()
    )


@pytest.fixture
def dry_ring_kit():
    store = _StoreWithDryRing()
    return _RingKit(
        _snapshot(), store, _FakePalette(), _FakeLookaheadHandle(store), _room_graph_pack()
    )


def test_deeper_expands_current_ring_then_resolves(capture_spans, unexpanded_ring_kit):  # noqa: F811
    """Bug B core: a ``deeper`` intent whose forward ring is unmaterialized must
    expand that ring and RESOLVE, not declare no_candidate_edges."""
    kit = unexpanded_ring_kit
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "press deeper"),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
            lookahead_handle=kit.lookahead_handle,
        )
    )
    # The ring expanded and the move resolved to the now-materialized deep room.
    assert out.data.get("resolved_via") not in (None,), out.data
    assert out.data.get("to_region") == _DEEP, out.data
    assert kit.snapshot.region_for(perspective="Groucho") == _DEEP, (
        "PC must advance into the freshly-expanded ring"
    )
    # The helper materialized the rooted edge via the real keyword contract
    # (edge rooted at the current node, to_region == the current node).
    assert kit.lookahead_handle.calls == [(_CUR, _CUR)], kit.lookahead_handle.calls
    # No engine-ran-dry span.
    unresolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.unresolved"]
    assert not unresolved, (
        f"engine ran dry instead of expanding the ring: {[s.attributes for s in unresolved]}"
    )


def test_deeper_with_dry_ring_still_fails_loud(capture_spans, dry_ring_kit):  # noqa: F811
    """No Silent Fallbacks: when the rooted frontier is empty (nothing to
    expand), the engine MUST still fail loud with no_candidate_edges — the ring
    expansion is a recovery for a genuinely-unexpanded ring, not a fallback that
    papers over a real dead end."""
    kit = dry_ring_kit
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "press deeper"),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
            lookahead_handle=kit.lookahead_handle,
        )
    )
    assert out.data.get("error") == "no_candidate_edges", out.data
    assert kit.snapshot.region_for(perspective="Groucho") == _CUR, (
        "a genuine dead end must not move the PC"
    )
    unresolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.unresolved"]
    assert len(unresolved) == 1, "expected exactly one movement.unresolved span"
    assert (unresolved[0].attributes or {}).get("reason") == "no_candidate_edges"
