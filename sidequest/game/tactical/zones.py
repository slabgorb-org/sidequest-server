"""Fate zone projection over a tactical mask (ADR-096 v2, Track C3).

Coarsen the same #/. grid the WN binding consumes as exact cells into
contiguous-cell-cluster ZONES the Fate binding consumes. Deterministic + pure:
zone ids are ``z0, z1, ...`` in scan order. Partition strategy: choke-seeded
multi-source flood — a cavern's own 1-wide chokepoints are the zone borders (a
Fate zone is a 'room/area'; a neck is exactly the border between two areas).
Non-chokepoint core cells (>2 orthogonal floor neighbours) form zone seeds via
8-connected components; a multi-source BFS over the FULL floor graph then
assigns every seed-reachable floor cell (chokes included) to its nearest seed,
ties breaking to the lexicographically-smallest zone-id string (deterministic;
note ``z10`` sorts before ``z2`` — room-scale grids stay under 10 zones).
Any floor component left with no cored seed (an isolated all-choke pocket or
corridor) then becomes its own zone, one per connected component — the
projection is TOTAL: every floor cell gets a home, no orphaned cells, and an
all-choke corridor or an open room degenerates cleanly to one zone.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from sidequest.game.tactical.adjudication import Cell, is_floor, neighbors, parse_mask


def _orth_floor_count(rows: list[str], cell: Cell) -> int:
    x, y = cell
    n = 0
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        if is_floor(rows, (x + dx, y + dy)):
            n += 1
    return n


@dataclass(frozen=True)
class ZoneProjection:
    """A total partition of the floor into named zones, plus zone adjacency."""

    zones: dict[str, frozenset[Cell]]
    cell_to_zone: dict[Cell, str]
    adjacency: dict[str, frozenset[str]]


@dataclass(frozen=True)
class ZoneMoveAdjudication:
    """A Fate-neutral verdict on one zone move (Track C3): same/adjacent zone is
    a free supplemental move; a non-adjacent (2+) move requires an Overcome."""

    free: bool
    requires_overcome: bool
    from_zone: str
    to_zone: str


def project_zones(mask: str) -> ZoneProjection:
    """Project the mask's floor into zones. Pure — no IO, no clock, no random."""
    rows = parse_mask(mask)
    floor: list[Cell] = [
        (x, y) for y, row in enumerate(rows) for x, ch in enumerate(row) if ch == "."
    ]
    if not floor:
        return ZoneProjection({}, {}, {})

    chokes = {c for c in floor if _orth_floor_count(rows, c) <= 2}
    core = [c for c in floor if c not in chokes]

    # Zone SEEDS = 8-connected components of the non-choke core cells (scan order).
    seed_zone: dict[Cell, str] = {}
    zid = 0
    for start in core:
        if start in seed_zone:
            continue
        name = f"z{zid}"
        zid += 1
        stack = [start]
        seed_zone[start] = name
        while stack:
            cur = stack.pop()
            for nb in neighbors(rows, cur):
                if nb in chokes or nb in seed_zone:
                    continue
                seed_zone[nb] = name
                stack.append(nb)

    # Assign every seed-reachable floor cell to its nearest seed-zone via
    # multi-source BFS over the full floor graph. All seeds start at layer 0;
    # FIFO layering gives the nearest zone, and seeding the frontier in
    # (zone-id-string, y, x) order makes ties break to the lexicographically-
    # smallest zone id — deterministic.
    zone_of: dict[Cell, str] = dict(seed_zone)
    frontier: deque[Cell] = deque(sorted(seed_zone, key=lambda c: (seed_zone[c], c[1], c[0])))
    while frontier:
        cur = frontier.popleft()
        for nb in neighbors(rows, cur):
            if nb not in zone_of:
                zone_of[nb] = zone_of[cur]
                frontier.append(nb)

    # TOTALITY: a floor component with no cored seed (an all-choke corridor or
    # an isolated all-choke pocket) is unreachable from every seed — flood each
    # remaining component as its own zone, in scan order, so no cell is ever
    # silently orphaned (review 165-5 round 1, [MEDIUM] G).
    for start in floor:
        if start in zone_of:
            continue
        name = f"z{zid}"
        zid += 1
        zone_of[start] = name
        stack = [start]
        while stack:
            cur = stack.pop()
            for nb in neighbors(rows, cur):
                if nb not in zone_of:
                    zone_of[nb] = name
                    stack.append(nb)

    zones: dict[str, set[Cell]] = {}
    for c, z in zone_of.items():
        zones.setdefault(z, set()).add(c)

    # Adjacency: two zones are adjacent iff a cell of one 8-touches a cell of the other.
    adjacency: dict[str, set[str]] = {z: set() for z in zones}
    for c, z in zone_of.items():
        for nb in neighbors(rows, c):
            nz = zone_of.get(nb)
            if nz is not None and nz != z:
                adjacency[z].add(nz)
                adjacency[nz].add(z)

    return ZoneProjection(
        zones={z: frozenset(cs) for z, cs in zones.items()},
        cell_to_zone=dict(zone_of),
        adjacency={z: frozenset(ns) for z, ns in adjacency.items()},
    )
