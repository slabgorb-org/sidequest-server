import pytest

from sidequest.dungeon.interiors.grid import FLOOR, WALL
from sidequest.dungeon.interiors.roomcorridor import gen_roomcorridor


def test_shape():
    g = gen_roomcorridor(width=40, height=30, seed=2)
    assert len(g) == 30 and all(len(r) == 40 for r in g)


def test_deterministic():
    assert gen_roomcorridor(width=40, height=30, seed=2) == gen_roomcorridor(
        width=40, height=30, seed=2
    )


def test_seed_variance():
    assert gen_roomcorridor(width=40, height=30, seed=2) != gen_roomcorridor(
        width=40, height=30, seed=3
    )


def test_borders_remain_walls():
    g = gen_roomcorridor(width=40, height=30, seed=2)
    for x in range(40):
        assert g[0][x] == WALL and g[29][x] == WALL
    for y in range(30):
        assert g[y][0] == WALL and g[y][39] == WALL


def test_has_at_least_two_rooms_worth_of_floor_and_is_connected():
    g = gen_roomcorridor(width=50, height=40, seed=7)
    h, w = len(g), len(g[0])
    floors = [(x, y) for y in range(h) for x in range(w) if g[y][x] == FLOOR]
    assert len(floors) > 30
    seen = {floors[0]}
    stack = [floors[0]]
    while stack:
        x, y = stack.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and g[ny][nx] == FLOOR and (nx, ny) not in seen:
                seen.add((nx, ny))
                stack.append((nx, ny))
    assert len(seen) == len(floors), "rooms not all corridor-connected"


def test_rejects_too_small():
    with pytest.raises(ValueError, match="width>=5 and height>=5"):
        gen_roomcorridor(width=4, height=4, seed=0)
