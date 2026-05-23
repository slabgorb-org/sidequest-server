"""Randomized-Prim variant maze generator.

Port of maze-maker lib/maze_maker/prim.rb. NOT classical priority-queue
Prim: it sows `density` seed points and extends each `complexity` steps
by carving to a random WALL two-step neighbor. May leave isolated
pockets by design — that is the documented maze-maker behavior.
"""

from __future__ import annotations

import random

from sidequest.dungeon.interiors.grid import (
    FLOOR,
    carve_between,
    new_grid,
    wall_neighbors,
)


def gen_prim(
    width: int,
    height: int,
    seed: int,
    *,
    density: int | None = None,
    complexity: int | None = None,
) -> list[list[int]]:
    """Deterministic for a given (width, height, seed, density, complexity).

    When density/complexity are None they scale with size, mirroring
    maze-maker's size-derived defaults.
    """
    if width < 3 or height < 3:
        raise ValueError(f"gen_prim requires width>=3 and height>=3; got {width}x{height}")
    rng = random.Random(seed)
    if density is None:
        density = max(1, (width + height) // 8)
    if complexity is None:
        complexity = max(1, (width + height) // 2)

    grid = new_grid(width, height)
    for _ in range(density):
        x = rng.randrange(0, max(1, (width - 1) // 2)) * 2 + 1
        y = rng.randrange(0, max(1, (height - 1) // 2)) * 2 + 1
        grid[y][x] = FLOOR
        for _ in range(complexity):
            candidates = wall_neighbors(grid, x, y)
            if not candidates:
                break
            nx, ny = candidates[rng.randrange(len(candidates))]
            carve_between(grid, x, y, nx, ny)
            x, y = nx, ny

    return grid
