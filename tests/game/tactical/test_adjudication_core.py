from sidequest.game.tactical.adjudication import (
    chebyshev_distance,
    in_bounds,
    is_floor,
    neighbors,
    parse_mask,
)

# 5x5: wall border, 3x3 floor interior, a wall pillar at (2,2).
MASK = "#####\n#...#\n#.#.#\n#...#\n#####"


def test_parse_mask_rows():
    rows = parse_mask(MASK)
    assert len(rows) == 5
    assert rows[0] == "#####"
    assert rows[1] == "#...#"


def test_is_floor_and_walls():
    rows = parse_mask(MASK)
    assert is_floor(rows, (1, 1))
    assert not is_floor(rows, (0, 0))  # wall
    assert not is_floor(rows, (2, 2))  # pillar
    assert not is_floor(rows, (9, 9))  # out of bounds


def test_in_bounds_edges():
    rows = parse_mask(MASK)
    assert in_bounds(rows, (0, 0))
    assert in_bounds(rows, (4, 4))
    assert not in_bounds(rows, (5, 0))
    assert not in_bounds(rows, (0, -1))


def test_chebyshev_diagonal_equals_orthogonal():
    assert chebyshev_distance((0, 0), (3, 0)) == 3
    assert chebyshev_distance((0, 0), (3, 3)) == 3  # diagonal same cost
    assert chebyshev_distance((1, 1), (1, 1)) == 0


def test_neighbors_excludes_walls_and_pillar():
    rows = parse_mask(MASK)
    ns = set(neighbors(rows, (1, 1)))
    assert (2, 1) in ns and (1, 2) in ns and (2, 2) not in ns  # pillar excluded
    assert (0, 0) not in ns  # wall excluded
    # centre-adjacent floor cell has the pillar removed from its 8 neighbours
    assert (2, 2) not in set(neighbors(rows, (3, 3)))
