"""Story 107-1 regression guard — an in-dungeon procedural move advances the
structured scene state.

Companion to tests/integration/test_dungeon_scene_advance_107_1.py (which covers
the per-room render emit). This locks in the engine-side half of the contract:
a successful move through the SINGLE movement-resolution mechanism
(``run_movement_dispatch``) must advance every field the descent's render
pipeline keys off — the exact behavior epic-107's forensics reported frozen
(and that #835 "in-dungeon movement for region-mode worlds" restored).

  1. discovered_regions grows by the entered region   (fog-of-war advance)
  2. region_transitions logs the entry                (the relocation receipt)
  3. current_region advances to the new room          (the render scene key)

There was NO prior test asserting these side effects fire together on a move;
the older movement-dispatch tests assert only ``pc_regions``. CONTENT-FREE:
synthetic graph + fake store + fake palette, mirroring test_movement_dispatch.py.
"""

from __future__ import annotations

import asyncio
import types

from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


class _FakePalette:
    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


class _FakeStore:
    def __init__(self, graph: RegionGraph):
        self._graph = graph

    def load_map(self, *, entrance_id: str) -> RegionGraph:
        return self._graph

    def load_frontier(self):
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

    # 1. fog-of-war advance
    assert "deep" in snap.discovered_regions, "entered region not added to discovered_regions"

    # 2. relocation receipt
    entries = [t for t in snap.region_transitions if t.to_region == "deep"]
    assert entries, "no region_transition logged for the entered room"
    assert entries[-1].from_region == "a"
    assert entries[-1].pc_name == "Rux"

    # 3. render scene key advances (consensus sync in apply_world_patch)
    assert snap.current_region == "deep", "current_region (render scene key) did not advance"
