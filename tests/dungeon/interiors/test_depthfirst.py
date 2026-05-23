import pytest

from sidequest.dungeon.interiors.depthfirst import gen_depthfirst
from sidequest.dungeon.interiors.grid import FLOOR, WALL


def test_shape():
    g = gen_depthfirst(width=21, height=21, seed=3)
    assert len(g) == 21 and all(len(r) == 21 for r in g)


def test_deterministic():
    assert gen_depthfirst(width=21, height=21, seed=3) == gen_depthfirst(
        width=21, height=21, seed=3
    )


def test_seed_variance():
    assert gen_depthfirst(width=21, height=21, seed=3) != gen_depthfirst(
        width=21, height=21, seed=4
    )


def test_value_domain_and_has_floor():
    g = gen_depthfirst(width=21, height=21, seed=3)
    assert all(c in (FLOOR, WALL) for row in g for c in row)
    assert any(c == FLOOR for row in g for c in row)


def test_perfect_maze_has_no_two_by_two_floor_block():
    g = gen_depthfirst(width=31, height=31, seed=11)
    h, w = len(g), len(g[0])
    for y in range(h - 1):
        for x in range(w - 1):
            block = (g[y][x], g[y][x + 1], g[y + 1][x], g[y + 1][x + 1])
            assert block != (FLOOR, FLOOR, FLOOR, FLOOR), f"loop at {x},{y}"


def test_all_floor_is_connected():
    g = gen_depthfirst(width=31, height=31, seed=11)
    h, w = len(g), len(g[0])
    start = next((x, y) for y in range(h) for x in range(w) if g[y][x] == FLOOR)
    seen = {start}
    stack = [start]
    while stack:
        x, y = stack.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and g[ny][nx] == FLOOR and (nx, ny) not in seen:
                seen.add((nx, ny))
                stack.append((nx, ny))
    assert len(seen) == sum(1 for r in g for c in r if c == FLOOR)


def test_rejects_too_small():
    with pytest.raises(ValueError, match="width>=3 and height>=3"):
        gen_depthfirst(width=1, height=1, seed=0)
