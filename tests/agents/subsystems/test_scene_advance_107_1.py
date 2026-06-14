"""Story 107-1 regression guard — an in-dungeon procedural move advances the
per-PC scene state, and the party-level scene key follows only on consensus.

Companion to tests/integration/test_dungeon_scene_advance_107_1.py (which covers
the per-room render-helper sourcing). This locks in the ENGINE half of the
contract: a successful move through the SINGLE movement-resolution mechanism
(``run_movement_dispatch``) advances the per-PC fields the descent's render
pipeline keys off — the behavior epic-107's forensics reported frozen (and that
#835 "in-dungeon movement for region-mode worlds" restored by making the
navigator reachable in-dungeon).

What a move advances, and how:
  1. pc_regions[mover]      — the per-PC region (direct, via the WorldStatePatch)
  2. discovered_regions     — fog-of-war (frontier_hook, on the per-PC transition)
  3. region_transitions     — the relocation receipt (apply_world_patch)
  4. current_region         — the PARTY scene key the render gate watches. This is
                              NOT a direct output of the movement mechanism: it is a
                              consensus-sync SIDE EFFECT inside apply_world_patch
                              (session.py:1546) that fires ONLY when the seated party
                              agrees on the new region. A SPLIT party leaves
                              current_region unchanged (test 2 below).

Scope/limits this guard does NOT cover (verified elsewhere or deferred):
  - that the server turn handler actually CALLS run_movement_dispatch on a player
    turn (the dispatch-bank wiring) — exercised by the dispatch-bank tests, not here.
  - that the render gate fires the location-description per room — that is the
    integration companion (helper-sourcing) plus the epic-107 live sq-playtest.

CONTENT-FREE: synthetic graph + fake store + fake palette, mirroring
test_movement_dispatch.py. The fakes stand in only for the dungeon store (graph
source) and palette (projection); the asserted side effects are produced by REAL
engine code (apply_world_patch → frontier_hook → consensus sync).
"""

from __future__ import annotations

import asyncio
import types

from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


class _FakePalette:
    def get(self, theme_id: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


class _FakeStore:
    def __init__(self, graph: RegionGraph) -> None:
        self._graph = graph

    def load_map(self, *, entrance_id: str) -> RegionGraph:
        return self._graph

    def load_frontier(self) -> list:
        # All target nodes are pre-committed in the test graph, so the §Q3
        # sync-materialize path (which consults the frontier) is intentionally
        # not exercised here — this guard covers the pre-materialized move.
        return []


def _graph() -> RegionGraph:
    g = RegionGraph(entrance_id="entrance")
    for nid, depth in [("entrance", 0.0), ("a", 1.0), ("deep", 5.0)]:
        g.add_node(RegionNode(id=nid, expansion_id=0, theme="t", depth_score=depth))
    g.add_edge(RegionEdge(a="entrance", b="a", kind="corridor", hidden=False))
    g.add_edge(RegionEdge(a="a", b="deep", kind="shaft", hidden=False))
    return g


def _dispatch(direction: str) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"direction": direction, "exit_descriptor": ""},
        idempotency_key="mv1",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


def test_in_dungeon_move_advances_scene_state():
    """Single-PC (full-consensus) descent: all four scene fields advance."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Rux": "a"},
        player_seats={"s1": "Rux"},
        current_region="a",
        discovered_regions=["entrance", "a"],
    )
    out = asyncio.run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FakeStore(_graph()),
            palette=_FakePalette(),
        )
    )
    assert out.data["to_region"] == "deep"
    assert snap.pc_regions["Rux"] == "deep"

    # 1. fog-of-war advance
    assert "deep" in snap.discovered_regions, "entered region not added to discovered_regions"

    # 2. relocation receipt — exactly one transition logged for this single move
    entries = [t for t in snap.region_transitions if t.to_region == "deep"]
    assert len(entries) == 1, f"expected exactly one region_transition for the move, got {entries}"
    assert entries[-1].from_region == "a"
    assert entries[-1].pc_name == "Rux"

    # 3. party scene key advances — single seated PC ⇒ consensus is guaranteed, so
    #    the apply_world_patch consensus-sync (session.py:1546) advances it. A split
    #    party would NOT advance it (see test_split_party_move below).
    assert snap.current_region == "deep", "current_region (render scene key) did not advance"


def test_split_party_move_does_not_advance_current_region():
    """Split party: the mover's per-PC fields advance, but the PARTY scene key
    (current_region) holds, because region_for() finds no consensus. This is the
    negative contract the single-PC test cannot show — a regression that advanced
    current_region on a split move (re-introducing the frozen/teleporting Location
    panel) would be caught here."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Rux": "a", "Marta": "a"},
        player_seats={"s1": "Rux", "s2": "Marta"},
        current_region="a",
        discovered_regions=["entrance", "a"],
    )
    out = asyncio.run(
        run_movement_dispatch(
            _dispatch(direction="deeper"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FakeStore(_graph()),
            palette=_FakePalette(),
        )
    )
    assert out.data["to_region"] == "deep"

    # The mover's per-PC state DID advance...
    assert snap.pc_regions["Rux"] == "deep"
    assert snap.pc_regions["Marta"] == "a"  # the other PC stayed put
    assert "deep" in snap.discovered_regions  # shared fog-of-war still grows
    assert any(t.to_region == "deep" and t.pc_name == "Rux" for t in snap.region_transitions)

    # ...but the PARTY scene key did NOT, because the party is split (no consensus).
    assert snap.current_region == "a", (
        "current_region must NOT advance on a split-party move — region_for() has no "
        "consensus, so the party scene key holds (split party = no shared scene)"
    )
