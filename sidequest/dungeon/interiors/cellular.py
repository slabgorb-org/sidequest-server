"""Cellular automata cavern generator.

Re-homed verbatim from the ADR-096 port
(sidequest-content/tools/cavern_renderer/cavern_renderer/cellular.py).
FLOOR/WALL now sourced from grid.py so all generators share constants.
"""

from __future__ import annotations

import random

from sidequest.dungeon.interiors.grid import FLOOR, WALL


def gen_cave(
    width: int,
    height: int,
    seed: int,
    *,
    density: float = 0.55,
    cutoff: int = 5,
    passes: int = 4,
) -> list[list[int]]:
    """Cellular-automaton cavern. Same (w,h,seed,density,cutoff,passes) → identical."""
    if width < 3 or height < 3:
        raise ValueError(f"gen_cave requires width>=3 and height>=3; got {width}x{height}")
    rng = random.Random(seed)
    grid = [
        [FLOOR if rng.random() < density else WALL for _ in range(width)] for _ in range(height)
    ]
    for x in range(width):
        grid[0][x] = WALL
        grid[height - 1][x] = WALL
    for y in range(height):
        grid[y][0] = WALL
        grid[y][width - 1] = WALL

    for _ in range(passes):
        grid = _ca_pass(grid, width, height, cutoff)

    return _keep_largest_floor_region(grid, width, height)


def _ca_pass(grid, width, height, cutoff):
    new = [row[:] for row in grid]
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            walls = 0
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    if grid[y + dy][x + dx] == WALL:
                        walls += 1
            if walls >= cutoff:
                new[y][x] = WALL
            elif walls < 4:
                new[y][x] = FLOOR
    return new


def _keep_largest_floor_region(grid, width, height):
    seen = [[False] * width for _ in range(height)]
    best: list[tuple[int, int]] = []
    for y in range(height):
        for x in range(width):
            if grid[y][x] != FLOOR or seen[y][x]:
                continue
            region = _flood(grid, seen, x, y, width, height)
            if len(region) > len(best):
                best = region
    keep = set(best)
    out = [row[:] for row in grid]
    for y in range(height):
        for x in range(width):
            if grid[y][x] == FLOOR and (x, y) not in keep:
                out[y][x] = WALL
    return out


def _flood(grid, seen, sx, sy, width, height):
    stack = [(sx, sy)]
    region: list[tuple[int, int]] = []
    while stack:
        x, y = stack.pop()
        if x < 0 or y < 0 or x >= width or y >= height:
            continue
        if seen[y][x] or grid[y][x] != FLOOR:
            continue
        seen[y][x] = True
        region.append((x, y))
        stack.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])
    return region
