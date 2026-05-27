from sidequest.game.dogfight_shot import (
    effective_armor_after_ap,
    resolve_geometry_modifier,
)
from sidequest.genre.models.rules import GeometryModifiers

GM = GeometryModifiers(
    aspect={"tail_on": 2, "quartering": 1, "crossing": -1, "head_on": -2},
    range={"gun": 2, "close": 0, "medium": -2, "far": -4},
)


def test_geometry_modifier_tail_on_gun_is_plus_4():
    state = {"target_aspect": "tail_on", "target_range": "gun"}
    assert resolve_geometry_modifier(state, GM) == 4


def test_geometry_modifier_head_on_far_is_minus_6():
    state = {"target_aspect": "head_on", "target_range": "far"}
    assert resolve_geometry_modifier(state, GM) == -6


def test_geometry_modifier_unknown_keys_contribute_zero():
    state = {"target_aspect": "inverted", "target_range": "gun"}
    assert resolve_geometry_modifier(state, GM) == 2  # only range matched


def test_geometry_modifier_missing_keys_contribute_zero():
    assert resolve_geometry_modifier({}, GM) == 0


def test_geometry_modifier_unknown_range_with_known_aspect():
    state = {"target_aspect": "tail_on", "target_range": "warp"}
    assert resolve_geometry_modifier(state, GM) == 2  # aspect matched, unknown range = 0


def test_effective_armor_full_penetration():
    assert effective_armor_after_ap(armor=5, armor_piercing=20) == 0


def test_effective_armor_partial():
    assert effective_armor_after_ap(armor=5, armor_piercing=2) == 3


def test_effective_armor_floor_zero():
    assert effective_armor_after_ap(armor=2, armor_piercing=10) == 0


def test_effective_armor_zero_ap_is_noop():
    assert effective_armor_after_ap(armor=5, armor_piercing=0) == 5
