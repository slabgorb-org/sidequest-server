from sidequest.game.tactical.adjudication import (
    adjudicate_move,
    cells_reachable,
    movement_cost,
)

# 1x7 corridor of floor between walls (row of ".", wall border top/bottom).
CORRIDOR = "#######\n#.....#\n#######"  # floor cells (1..5, row 1)
# 5x5 open room with wall pillar at (2,2).
ROOM = "#####\n#...#\n#.#.#\n#...#\n#####"


def test_reachable_budget_limits_distance():
    r = cells_reachable((1, 1), 2, CORRIDOR)
    assert (3, 1) in r.reachable  # 2 cells away
    assert (4, 1) not in r.reachable  # 3 cells away, over budget
    assert (1, 1) not in r.reachable  # origin excluded


def test_reachable_zero_budget_empty():
    assert cells_reachable((1, 1), 0, CORRIDOR).reachable == frozenset()


def test_reachable_walls_block():
    # Pillar at (2,2) is never reachable; open cells around it are.
    r = cells_reachable((1, 1), 4, ROOM)
    assert (2, 2) not in r.reachable
    assert (3, 3) in r.reachable


def test_reachable_difficult_terrain_doubles_cost():
    # Entering (3,1) is difficult -> costs 2, so with budget 2 you can still
    # stop on it, but (4,1) beyond it now costs 3 and is out of reach.
    r = cells_reachable((1, 1), 2, CORRIDOR, difficult=frozenset({(3, 1)}))
    assert r.cost[(2, 1)] == 1
    assert r.cost[(3, 1)] == 3  # 1 (to (2,1)) + 2 (enter difficult (3,1))
    assert (3, 1) not in r.reachable  # cost 3 > budget 2


def test_movement_cost_straight_and_diagonal():
    assert movement_cost([(1, 1), (2, 1), (3, 1)], ROOM) == 2
    assert movement_cost([(1, 1), (2, 1)], ROOM) == 1
    # diagonal step is one cell (Chebyshev); stepping onto a wall is illegal -> None
    assert movement_cost([(1, 1), (2, 2)], "###\n#..\n#.#") is None  # (2,2) is a wall here


def test_movement_cost_rejects_non_adjacent_and_walls():
    assert movement_cost([(1, 1), (3, 1)], ROOM) is None  # jump, not adjacent
    assert movement_cost([(1, 1), (2, 2)], ROOM) is None  # (2,2) is the pillar (wall)
    assert movement_cost([(1, 1)], ROOM) == 0  # single floor cell = no move
    assert movement_cost([], ROOM) is None


def test_adjudicate_move_valid_and_denied():
    ok = adjudicate_move(origin=(1, 1), path=[(1, 1), (2, 1), (3, 1)], budget_cells=6, mask=ROOM)
    assert ok.valid and ok.cells_spent == 2 and ok.cells_budget == 6 and ok.reason == ""
    over = adjudicate_move(origin=(1, 1), path=[(1, 1), (2, 1), (3, 1)], budget_cells=1, mask=ROOM)
    assert not over.valid and over.cells_spent == 2 and "1 cell" in over.reason
    bad = adjudicate_move(origin=(1, 1), path=[(1, 1), (3, 1)], budget_cells=6, mask=ROOM)
    assert not bad.valid and bad.cells_spent == 0 and "path" in bad.reason.lower()
