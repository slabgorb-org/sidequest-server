from sidequest.dungeon.tactical import (
    RegionTactical,
    derive_region_tactical,
)

# A 5x5 room: border walls, floor interior, a 1-wide neck at row 2.
GRID = [
    [1, 1, 1, 1, 1],
    [1, 0, 0, 0, 1],
    [1, 1, 0, 1, 1],  # neck: only (2,2) is floor in this row
    [1, 0, 0, 0, 1],
    [1, 1, 1, 1, 1],
]


def test_determinism_same_seed_same_output():
    a = derive_region_tactical(
        region_id="exp001.r0", grid=GRID, theme_key="drowned_cavern",
        neighbor_ids=["entrance"], hazard_setpieces=[], creature_count=2,
    )
    b = derive_region_tactical(
        region_id="exp001.r0", grid=GRID, theme_key="drowned_cavern",
        neighbor_ids=["entrance"], hazard_setpieces=[], creature_count=2,
    )
    assert a == b
    assert isinstance(a, RegionTactical)


def test_chokepoint_detected_as_difficult_terrain():
    t = derive_region_tactical(
        region_id="r", grid=GRID, theme_key="bone_crypt",
        neighbor_ids=[], hazard_setpieces=[], creature_count=0,
    )
    choke_cells = {f.cell for f in t.features if f.feature_type == "difficult_terrain"}
    assert (2, 2) in choke_cells  # the 1-wide neck


def test_water_theme_floods_some_floor():
    t = derive_region_tactical(
        region_id="r", grid=GRID, theme_key="drowned_cavern",
        neighbor_ids=[], hazard_setpieces=[], creature_count=0,
    )
    assert any(f.feature_type == "water" for f in t.features)


def test_non_water_theme_no_water():
    t = derive_region_tactical(
        region_id="r", grid=GRID, theme_key="bone_crypt",
        neighbor_ids=[], hazard_setpieces=[], creature_count=0,
    )
    assert not any(f.feature_type == "water" for f in t.features)


def test_anchors_on_floor_and_counts_match():
    t = derive_region_tactical(
        region_id="r", grid=GRID, theme_key="bone_crypt",
        neighbor_ids=[], hazard_setpieces=[], creature_count=3,
    )
    assert sum(1 for a in t.anchors if a.role == "entrance") == 1
    assert sum(1 for a in t.anchors if a.role == "creature") == 3
    for a in t.anchors:
        x, y = a.cell
        assert GRID[y][x] == 0  # every anchor is on floor


def test_hazard_setpiece_places_hazard():
    t = derive_region_tactical(
        region_id="r", grid=GRID, theme_key="bone_crypt",
        neighbor_ids=[], hazard_setpieces=["collapse_gallery"], creature_count=0,
    )
    assert any(f.feature_type == "hazard" for f in t.features)
