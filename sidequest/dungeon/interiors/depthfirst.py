"""Recursive-backtracker (depth-first) maze generator.

Port of maze-maker lib/maze_maker/depthfirst.rb. Produces a perfect
maze (exactly one path between any two FLOOR cells; zero loops).
"""

from __future__ import annotations

import random

from sidequest.dungeon.interiors.grid import (
    FLOOR,
    carve_between,
    new_grid,
    wall_neighbors,
)


def gen_depthfirst(width: int, height: int, seed: int) -> list[list[int]]:
    """Deterministic for a given (width, height, seed)."""
    if width < 3 or height < 3:
        raise ValueError(f"gen_depthfirst requires width>=3 and height>=3; got {width}x{height}")
    rng = random.Random(seed)
    grid = new_grid(width, height)

    sx = rng.randrange(0, max(1, (width - 1) // 2)) * 2 + 1
    sy = rng.randrange(0, max(1, (height - 1) // 2)) * 2 + 1
    grid[sy][sx] = FLOOR
    stack: list[tuple[int, int]] = [(sx, sy)]

    while stack:
        x, y = stack[-1]
        candidates = wall_neighbors(grid, x, y)
        if candidates:
            nx, ny = candidates[rng.randrange(len(candidates))]
            carve_between(grid, x, y, nx, ny)
            stack.append((nx, ny))
        else:
            stack.pop()

    return grid
