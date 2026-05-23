"""Coordinator: dispatch by algorithm name, apply optional braid.

Single entry point for the materializer (Plan 7) and the authoring
CLI (Task 8). Unknown algorithm raises loudly — no silent fallback
(CLAUDE.md: No Silent Fallbacks).
"""

from __future__ import annotations

from sidequest.dungeon.interiors.braid import braid
from sidequest.dungeon.interiors.cellular import gen_cave
from sidequest.dungeon.interiors.depthfirst import gen_depthfirst
from sidequest.dungeon.interiors.grid import Grid
from sidequest.dungeon.interiors.prim import gen_prim
from sidequest.dungeon.interiors.roomcorridor import gen_roomcorridor

ALGORITHMS = {
    "cellular": gen_cave,
    "depthfirst": gen_depthfirst,
    "prim": gen_prim,
    "roomcorridor": gen_roomcorridor,
}


def generate_interior(
    algorithm: str,
    *,
    width: int,
    height: int,
    seed: int,
    braid_ratio: float = 0.0,
    params: dict | None = None,
) -> Grid:
    """Generate one interior grid. Deterministic for identical inputs."""
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unknown interior algorithm {algorithm!r}; known: {sorted(ALGORITHMS)}")
    fn = ALGORITHMS[algorithm]
    grid = fn(width=width, height=height, seed=seed, **(params or {}))
    if braid_ratio > 0.0:
        # Distinct seed so the braid RNG is independent of the map-carving RNG.
        grid = braid(grid, seed=seed ^ 0x5EED, braid_ratio=braid_ratio)
    return grid
