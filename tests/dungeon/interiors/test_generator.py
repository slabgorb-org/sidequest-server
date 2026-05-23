import pytest

from sidequest.dungeon.interiors.generator import ALGORITHMS, generate_interior
from sidequest.dungeon.interiors.grid import FLOOR, WALL


def test_registry_lists_all_four_algorithms():
    assert set(ALGORITHMS) == {"cellular", "depthfirst", "prim", "roomcorridor"}


@pytest.mark.parametrize("algo", ["cellular", "depthfirst", "prim", "roomcorridor"])
def test_each_algorithm_produces_valid_deterministic_grid(algo):
    g1 = generate_interior(algo, width=25, height=25, seed=42)
    g2 = generate_interior(algo, width=25, height=25, seed=42)
    assert g1 == g2
    assert len(g1) == 25 and all(len(r) == 25 for r in g1)
    assert all(c in (FLOOR, WALL) for row in g1 for c in row)


def test_unknown_algorithm_raises_loudly():
    with pytest.raises(ValueError, match="unknown interior algorithm 'spelunk'"):
        generate_interior("spelunk", width=10, height=10, seed=1)


def test_braid_ratio_applied_for_maze_algorithms():
    plain = generate_interior("depthfirst", width=41, height=41, seed=4)
    braided = generate_interior("depthfirst", width=41, height=41, seed=4, braid_ratio=1.0)
    from sidequest.dungeon.interiors.braid import dead_ends

    assert dead_ends(braided) == []
    assert plain != braided


def test_params_passed_through():
    a = generate_interior(
        "prim", width=31, height=31, seed=9, params={"density": 3, "complexity": 10}
    )
    b = generate_interior(
        "prim", width=31, height=31, seed=9, params={"density": 3, "complexity": 10}
    )
    c = generate_interior(
        "prim", width=31, height=31, seed=9, params={"density": 12, "complexity": 40}
    )
    assert a == b
    assert a != c


def test_public_api_re_exports():
    from sidequest.dungeon import interiors

    assert hasattr(interiors, "generate_interior")
    assert hasattr(interiors, "ALGORITHMS")
    assert interiors.generate_interior is generate_interior
