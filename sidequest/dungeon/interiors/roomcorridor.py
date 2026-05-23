"""Room-and-corridor generator (built themes: temple, vault, hall).

NEW — no maze-maker equivalent. Places rectangular rooms (rooms MAY
overlap — this is intentional, producing organic interconnected
chambers) and connects consecutive room centers with L-shaped
corridors. Corridors may cross, creating loops; that is intended for
built themes. Practical minimum useful size is roughly 12x12 with the
default room params; below that the boundary check may reject every
room and return an all-wall grid.
"""

from __future__ import annotations

import random

from sidequest.dungeon.interiors.grid import FLOOR, WALL, new_grid


def _carve_room(grid, x0, y0, rw, rh):
    for y in range(y0, y0 + rh):
        for x in range(x0, x0 + rw):
            grid[y][x] = FLOOR


def _carve_h(grid, x_a, x_b, y):
    for x in range(min(x_a, x_b), max(x_a, x_b) + 1):
        grid[y][x] = FLOOR


def _carve_v(grid, y_a, y_b, x):
    for y in range(min(y_a, y_b), max(y_a, y_b) + 1):
        grid[y][x] = FLOOR


def gen_roomcorridor(
    width: int,
    height: int,
    seed: int,
    *,
    max_rooms: int = 12,
    room_min: int = 3,
    room_max: int = 7,
) -> list[list[int]]:
    """Deterministic for a given (width, height, seed, max_rooms, room_min, room_max)."""
    if width < 5 or height < 5:
        raise ValueError(f"gen_roomcorridor requires width>=5 and height>=5; got {width}x{height}")
    rng = random.Random(seed)
    grid = new_grid(width, height)
    centers: list[tuple[int, int]] = []

    for _ in range(max_rooms):
        rw = rng.randint(room_min, room_max)
        rh = rng.randint(room_min, room_max)
        x0 = rng.randint(1, max(1, width - rw - 1))
        y0 = rng.randint(1, max(1, height - rh - 1))
        if x0 + rw >= width - 1 or y0 + rh >= height - 1:
            continue
        _carve_room(grid, x0, y0, rw, rh)
        cx, cy = x0 + rw // 2, y0 + rh // 2
        if centers:
            px, py = centers[-1]
            if rng.random() < 0.5:
                _carve_h(grid, px, cx, py)
                _carve_v(grid, py, cy, cx)
            else:
                _carve_v(grid, py, cy, px)
                _carve_h(grid, px, cx, cy)
        centers.append((cx, cy))

    for x in range(width):
        grid[0][x] = WALL
        grid[height - 1][x] = WALL
    for y in range(height):
        grid[y][0] = WALL
        grid[y][width - 1] = WALL
    return grid
