"""Region-exit bearings (sq-playtest 2026-06-13).

The procedural region graph is an abstract topology — nodes + (a, b, kind)
edges, never embedded in a plane — so a per-edge DIRECTION was never assigned,
which is why "I go north" / "the corridor ahead" could not resolve and the
narrator was driven to confabulate. ``assign_bearings`` gives each region's
exits a stable, DISTINCT bearing (compass for passages, up/down for vertical
ways), derived purely from the graph so the narrator prompt, the DUNGEON_MAP
frame, and the movement resolver all agree without persistence.

Content-free: synthetic ``RegionGraph`` + a duck-typed palette.
"""

from __future__ import annotations

import types

from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.dungeon.region_projection import (
    COMPASS_ORDER,
    assign_bearings,
    project_region,
    requested_bearing,
)


class _FakePalette:
    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


def _graph(nodes, edges, entrance="entrance") -> RegionGraph:
    g = RegionGraph(entrance_id=entrance)
    for nid, depth in nodes:
        g.add_node(RegionNode(id=nid, expansion_id=0, theme="t", depth_score=depth))
    for a, b, kind in edges:
        g.add_edge(RegionEdge(a=a, b=b, kind=kind))
    return g


def test_bearings_are_distinct_for_the_four_way_tie():
    # The exact playtest shape: entrance with three corridors + one shaft —
    # the case that tied on "the corridor ahead". Every exit must get a
    # distinct bearing so the narrator can name them apart.
    g = _graph(
        [("entrance", 0.0), ("r0", 1.0), ("r1", 1.0), ("r2", 2.0), ("deep", 3.0)],
        [
            ("entrance", "r0", "corridor"),
            ("entrance", "r1", "corridor"),
            ("entrance", "r2", "corridor"),
            ("entrance", "deep", "shaft"),
        ],
    )
    bearings = assign_bearings(g, "entrance")
    assert len(bearings) == 4
    assert len(set(bearings.values())) == 4, f"bearings not distinct: {bearings}"
    # the shaft descends → "down"; the corridors take compass points.
    assert bearings["deep"] == "down"
    assert all(bearings[c] in COMPASS_ORDER for c in ("r0", "r1", "r2"))


def test_bearings_are_stable_across_calls():
    # blake2b-seeded, not Python hash() — identical across calls/processes.
    g = _graph(
        [("entrance", 0.0), ("a", 1.0), ("b", 1.0), ("c", 1.0)],
        [
            ("entrance", "a", "corridor"),
            ("entrance", "b", "corridor"),
            ("entrance", "c", "corridor"),
        ],
    )
    assert assign_bearings(g, "entrance") == assign_bearings(g, "entrance")


def test_vertical_kinds_take_up_or_down_by_depth():
    g = _graph(
        [("mid", 2.0), ("below", 3.0), ("above", 1.0)],
        [("mid", "below", "stairs"), ("mid", "above", "stairs")],
        entrance="above",
    )
    bearings = assign_bearings(g, "mid")
    assert bearings["below"] == "down"
    assert bearings["above"] == "up"


def test_project_region_populates_bearing_on_each_exit():
    g = _graph(
        [("entrance", 0.0), ("a", 1.0), ("deep", 2.0)],
        [("entrance", "a", "corridor"), ("entrance", "deep", "shaft")],
    )
    proj = project_region(g, "entrance", _FakePalette())
    by_id = {e.to_region_id: e for e in proj.exits}
    assert by_id["deep"].bearing == "down"
    assert by_id["a"].bearing in COMPASS_ORDER
    # every exit carries a bearing — none left blank.
    assert all(e.bearing for e in proj.exits)


def test_requested_bearing_token_exact_not_substring():
    assert requested_bearing("I go north") == "north"
    assert requested_bearing("down the shaft") == "down"
    assert requested_bearing("climb the stairs up") == "up"
    # compound diagonal wins over the cardinal inside it.
    assert requested_bearing("the northeast tunnel") == "northeast"
    # a coarse/relative word names no bearing → None (the deeper/back path owns it).
    assert requested_bearing("I follow the corridor ahead") is None
    assert requested_bearing("go deeper") is None
    # not a spurious substring match: "upper" must not read as "up".
    assert requested_bearing("the upper vault") is None
