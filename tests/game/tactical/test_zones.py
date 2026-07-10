"""RED tests for Task 11 — pure Fate zone projection (ADR-096 v2, Track C3).

``project_zones(mask)`` coarsens the same #/. grid the WN binding consumes as
exact cells into contiguous-cell-cluster ZONES for the Fate binding. Contract
(plan Task 11, hand-verified against the merged C1 library per the 165-1
carryover — the plan doc's embedded code is not authoritative):

- Pure + deterministic: stable zone ids ``z0, z1, ...`` assigned in scan order.
- Choke-seeded multi-source flood: non-chokepoint core cells (>2 orthogonal
  floor neighbours) form zone seeds via 8-connected components; a multi-source
  BFS over the FULL floor graph then assigns EVERY floor cell (chokes included)
  to its nearest seed — no orphaned cells.
- Degenerates cleanly: an all-choke corridor or an open room is ONE zone.
- ``ZoneMoveAdjudication(free, requires_overcome, from_zone, to_zone)`` is
  defined HERE (Task 11) so the Fate binding (Task 12) imports it, never
  redefines it.

Hand-verified DUMBBELL partition (orthogonal-floor counts computed by hand):
cores are exactly (3,1) and (3,3) — every other floor cell has <=2 orthogonal
floor neighbours — so the cavern splits into a TOP lobe (z0, seeded at (3,1))
and a BOTTOM lobe (z1, seeded at (3,3)), 8 + 5 = 13 floor cells total.
"""

from __future__ import annotations

import dataclasses

import pytest
from sidequest.game.tactical.zones import (
    ZoneMoveAdjudication,
    ZoneProjection,
    project_zones,
)

# A cavern whose middle row is pinched by wall pillars at (2,2) and (4,2),
# leaving 1-wide choke columns between the top and bottom rows: a natural
# multi-zone cavern. 13 floor cells.
DUMBBELL = "#######\n#.....#\n#.#.#.#\n#.....#\n#######"

OPEN_ROOM = "#####\n#...#\n#...#\n#####"

# Every floor cell in a 1-wide corridor has <=2 orthogonal floor neighbours —
# all-choke, no seeds — the stated degenerate case.
CORRIDOR = "#######\n#.....#\n#######"

ALL_WALL = "#####\n#####\n#####"


def _floor_cells(mask: str) -> set[tuple[int, int]]:
    return {
        (x, y) for y, row in enumerate(mask.split("\n")) for x, ch in enumerate(row) if ch == "."
    }


# --- Determinism + totality ----------------------------------------------------------


def test_projection_is_deterministic():
    a = project_zones(DUMBBELL)
    b = project_zones(DUMBBELL)
    assert isinstance(a, ZoneProjection)
    assert a.cell_to_zone == b.cell_to_zone
    assert a.zones == b.zones
    assert a.adjacency == b.adjacency


def test_every_floor_cell_has_a_zone():
    """Totality: chokes included — the multi-source BFS orphans nothing."""
    proj = project_zones(DUMBBELL)
    floor = _floor_cells(DUMBBELL)
    assert set(proj.cell_to_zone) == floor
    covered = set()
    for cells in proj.zones.values():
        covered |= cells
    assert covered == floor


def test_no_wall_cell_gets_a_zone():
    proj = project_zones(DUMBBELL)
    floor = _floor_cells(DUMBBELL)
    for cell in proj.cell_to_zone:
        assert cell in floor, f"non-floor cell {cell} was assigned a zone"


def test_zones_and_cell_to_zone_are_consistent():
    """The two views are the same partition: c in zones[z] iff cell_to_zone[c]==z."""
    proj = project_zones(DUMBBELL)
    for zone_id, cells in proj.zones.items():
        for cell in cells:
            assert proj.cell_to_zone[cell] == zone_id
    for cell, zone_id in proj.cell_to_zone.items():
        assert cell in proj.zones[zone_id]


# --- Partition shape (hand-verified) ---------------------------------------------------


def test_dumbbell_partitions_into_two_zones_with_stable_ids():
    """Cores are exactly (3,1) and (3,3) — two seeds, scan-order ids z0/z1.
    The pinched middle row separates the top lobe from the bottom lobe."""
    proj = project_zones(DUMBBELL)
    assert set(proj.zones) == {"z0", "z1"}
    # Top-left and bottom-left floor cells land in DIFFERENT zones (the pinch
    # is the border) — the mechanically-load-bearing fact for Fate zone moves.
    assert proj.cell_to_zone[(1, 1)] != proj.cell_to_zone[(1, 3)]
    # Scan order: (3,1) is discovered before (3,3), so the top lobe is z0.
    assert proj.cell_to_zone[(3, 1)] == "z0"
    assert proj.cell_to_zone[(3, 3)] == "z1"


def test_open_room_is_one_zone():
    proj = project_zones(OPEN_ROOM)
    assert len(proj.zones) == 1
    assert set(proj.cell_to_zone) == _floor_cells(OPEN_ROOM)
    # A lone zone has no neighbours — and no self-loop.
    assert proj.adjacency == {"z0": frozenset()}


def test_all_choke_corridor_degenerates_to_one_zone():
    """No core cells at all -> the whole floor is one zone (stated contract:
    'degenerates cleanly to one zone for an all-choke corridor')."""
    proj = project_zones(CORRIDOR)
    assert set(proj.zones) == {"z0"}
    assert set(proj.cell_to_zone) == _floor_cells(CORRIDOR)
    assert proj.adjacency == {"z0": frozenset()}


def test_no_floor_mask_is_empty_projection():
    proj = project_zones(ALL_WALL)
    assert proj.zones == {}
    assert proj.cell_to_zone == {}
    assert proj.adjacency == {}


# --- Adjacency --------------------------------------------------------------------------


def test_adjacency_is_symmetric():
    proj = project_zones(DUMBBELL)
    for zone_id, neighbours in proj.adjacency.items():
        for n in neighbours:
            assert zone_id in proj.adjacency[n], f"{zone_id}->{n} not symmetric"


def test_adjacency_has_no_self_loops():
    proj = project_zones(DUMBBELL)
    for zone_id, neighbours in proj.adjacency.items():
        assert zone_id not in neighbours, f"{zone_id} is adjacent to itself"


def test_dumbbell_lobes_are_adjacent():
    """The two lobes touch through the choke columns — a one-zone move between
    them must classify as adjacent (Fate RAW: a free supplemental move)."""
    proj = project_zones(DUMBBELL)
    top = proj.cell_to_zone[(1, 1)]
    bottom = proj.cell_to_zone[(1, 3)]
    assert bottom in proj.adjacency[top]


# --- Immutability of the projection ---------------------------------------------------


def test_projection_zone_sets_are_frozen():
    proj = project_zones(DUMBBELL)
    for cells in proj.zones.values():
        assert isinstance(cells, frozenset)
    for neighbours in proj.adjacency.values():
        assert isinstance(neighbours, frozenset)


# --- ZoneMoveAdjudication lives HERE (Task 11) so Task 12 imports it -------------------


def test_zone_move_adjudication_is_a_frozen_verdict():
    verdict = ZoneMoveAdjudication(free=True, requires_overcome=False, from_zone="z0", to_zone="z1")
    assert verdict.free is True
    assert verdict.requires_overcome is False
    assert verdict.from_zone == "z0"
    assert verdict.to_zone == "z1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.free = False  # type: ignore[misc]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
