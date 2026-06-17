"""Tests for the dial/confrontation resolution behind the RulesetModule seam.

Pins the absolute behavior of ``DialRulesetModule`` for stat_modifier and
compute_dc so no refactor can silently change the resolution math.
"""

import pytest

from sidequest.game.ruleset.dial import DialRulesetModule
from sidequest.genre.models.rules import BeatDef

_NATIVE = DialRulesetModule()


def test_dial_slug_is_dial() -> None:
    assert DialRulesetModule.slug == "dial"


@pytest.mark.parametrize(
    "score,expected",
    [(10, 0), (12, 1), (8, -1), (18, 4), (3, -4), (20, 5)],
)
def test_dial_stat_modifier_dnd_formula(score: int, expected: int) -> None:
    assert _NATIVE.stat_modifier({"STR": score}, "STR") == expected


def test_dial_stat_modifier_missing_stat_defaults_to_zero() -> None:
    assert _NATIVE.stat_modifier({}, "STR") == 0


def test_dial_stat_modifier_case_insensitive() -> None:
    # stat stored lowercase, queried uppercase -> must still resolve via fallback
    assert _NATIVE.stat_modifier({"str": 14}, "STR") == 2
    # and the reverse
    assert _NATIVE.stat_modifier({"STR": 14}, "str") == 2


@pytest.mark.parametrize(
    "base,expected_dc",
    [(0, 10), (1, 12), (5, 20), (10, 30), (15, 30)],
)
def test_dial_compute_dc_clamped(base: int, expected_dc: int) -> None:
    beat = BeatDef(id="b", label="B", kind="strike", base=base, stat_check="STR")
    assert _NATIVE.compute_dc(beat) == expected_dc
