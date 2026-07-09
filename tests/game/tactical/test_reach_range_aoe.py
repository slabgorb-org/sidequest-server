import pytest

from sidequest.game.tactical.adjudication import (
    adjudicate_reach,
    aoe_burst,
    aoe_line,
    line_of_sight,
    reach_cells,
)

ROOM = "#####\n#...#\n#.#.#\n#...#\n#####"  # pillar wall at (2,2)
OPEN = "......\n......\n......\n......"  # 6x4 all floor
WALLED = "#######\n#..#..#\n#######"  # wall at (3,1) between (1,1) and (4,1)


def test_reach_cells_adjacent_only_at_reach_1():
    r = reach_cells((1, 1), 1, ROOM)
    assert (2, 1) in r and (1, 2) in r
    assert (3, 1) not in r  # 2 away
    assert (2, 2) not in r  # pillar (wall)
    assert (1, 1) not in r  # origin excluded


def test_reach_cells_zero_reach_empty():
    assert reach_cells((1, 1), 0, ROOM) == frozenset()


def test_line_of_sight_clear_and_blocked():
    assert line_of_sight((1, 1), (4, 1), OPEN) is True
    assert line_of_sight((1, 1), (4, 1), WALLED) is False  # wall at (3,1)


def test_adjudicate_reach_melee_in_and_out():
    hit = adjudicate_reach(
        origin=(1, 1), target=(2, 1), max_cells=1, mask=ROOM, mode="melee", require_los=False
    )
    assert hit.in_range and hit.distance_cells == 1 and hit.reason == ""
    miss = adjudicate_reach(
        origin=(1, 1), target=(3, 3), max_cells=1, mask=ROOM, mode="melee", require_los=False
    )
    assert not miss.in_range and miss.distance_cells == 2 and "reach" in miss.reason.lower()


def test_adjudicate_reach_ranged_requires_los():
    blocked = adjudicate_reach(
        origin=(1, 1), target=(4, 1), max_cells=40, mask=WALLED, mode="ranged", require_los=True
    )
    assert not blocked.in_range and blocked.has_los is False and "sight" in blocked.reason.lower()
    clear = adjudicate_reach(
        origin=(1, 1), target=(4, 1), max_cells=40, mask=OPEN, mode="ranged", require_los=True
    )
    assert clear.in_range and clear.has_los is True


def test_aoe_burst_radius_and_los_shadow():
    cells = aoe_burst((2, 1), 1, OPEN)
    assert (2, 1) in cells and (1, 1) in cells and (3, 1) in cells
    # a wall shadows cells behind it from the centre
    shadowed = aoe_burst((1, 1), 3, WALLED)
    assert (4, 1) not in shadowed  # behind the (3,1) wall


def test_aoe_line_stops_at_wall():
    line = aoe_line((1, 1), (5, 1), WALLED)
    assert (1, 1) in line and (2, 1) in line
    assert (4, 1) not in line  # ray stopped by the (3,1) wall


# --- 165-3: adjudicate_reach mode-validation (165-1 tightening) -----------------------


def test_adjudicate_reach_rejects_unknown_mode():
    """165-1 carryover, RESOLVED here: ``adjudicate_reach(mode)`` was unvalidated,
    so an unknown mode silently fell through to the 'range' noun (the
    No-Silent-Fallbacks tension the 165-1 reviewer flagged). As 165-3 wires this
    adjudicator into the production strike path, an unknown mode must FAIL LOUD —
    ``mode`` is a closed {'melee','ranged'} vocabulary, not free text."""
    with pytest.raises(ValueError):
        adjudicate_reach(
            origin=(1, 1),
            target=(2, 1),
            max_cells=1,
            mask=ROOM,
            mode="broadsword",  # neither 'melee' nor 'ranged'
            require_los=False,
        )


# --- 165-3: 165-1 coverage backfill (characterization of already-shipped C1) ----------
# These pin behaviour the reach/AoE enforcement now depends on. Per the 165-1
# carryover ("close the coverage gaps as you consume the library"). They exercise
# EXISTING correct C1 behaviour, so they pass green — regression guards, not new ACs.


def test_line_of_sight_excludes_endpoints():
    """LOS checks only cells STRICTLY between origin and target (``ray[1:-1]``):
    a wall glyph AT an endpoint never blocks, so a shooter/target standing at
    cover's edge still sees out. 165-1 gap: the endpoint-exclusion branch was
    untested. (Off-map is still blocking — a separate branch.)"""
    # (1,1),(2,1) floor; (3,1) is a wall sitting exactly on the target endpoint.
    mask = "#####\n#..#.\n#####"
    assert line_of_sight((1, 1), (3, 1), mask) is True  # endpoint wall ignored
    # Contrast: a wall in the INTERIOR of the ray does block.
    interior = "######\n#.##..\n######"  # (2,1),(3,1) walls between (1,1) and (4,1)
    assert line_of_sight((1, 1), (4, 1), interior) is False


def test_aoe_burst_ignores_los_when_disabled():
    """``require_los=False`` lights every in-radius floor cell, even those a wall
    would shadow (a cover-ignoring burst — gas/expanding effects). 165-1 gap: the
    ``require_los=False`` branch was untested; the existing burst test only covers
    the default LOS-shadowed path."""
    lit = aoe_burst((1, 1), 3, WALLED, require_los=False)
    assert (4, 1) in lit  # behind the (3,1) wall, but LOS gating is OFF
    shadowed = aoe_burst((1, 1), 3, WALLED, require_los=True)
    assert (4, 1) not in shadowed  # contrast: default LOS gate shadows it
