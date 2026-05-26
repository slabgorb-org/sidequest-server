"""Tests for native dial/confrontation resolution behind the RulesetModule seam.

This file holds BOTH:
  * characterization tests that lock the current behavior of the legacy
    ``_stat_modifier`` and ``_compute_dc`` free-functions in dice.py, and
  * equivalence tests proving ``NativeRulesetModule`` reproduces those functions
    exactly (it wraps/delegates rather than relocating them).
"""

import pytest

from sidequest.genre.models.rules import BeatDef
from sidequest.server.dispatch.dice import _compute_dc, _stat_modifier


@pytest.mark.parametrize(
    "score,expected",
    [(10, 0), (12, 1), (8, -1), (18, 4), (3, -4), (20, 5)],
)
def test_stat_modifier_dnd_formula(score: int, expected: int) -> None:
    assert _stat_modifier({"STR": score}, "STR") == expected


def test_stat_modifier_missing_stat_defaults_to_zero() -> None:
    assert _stat_modifier({}, "STR") == 0


@pytest.mark.parametrize(
    "base,expected_dc",
    [(0, 10), (1, 12), (5, 20), (10, 30), (15, 30)],
)
def test_compute_dc_clamped(base: int, expected_dc: int) -> None:
    beat = BeatDef(id="b", label="B", kind="strike", base=base, stat_check="STR")
    assert _compute_dc(beat) == expected_dc


def test_stat_modifier_case_insensitive_lookup() -> None:
    # stat stored lowercase, queried uppercase -> must still resolve via fallback
    assert _stat_modifier({"str": 14}, "STR") == 2
    # and the reverse
    assert _stat_modifier({"STR": 14}, "str") == 2


# ---------------------------------------------------------------------------
# NativeRulesetModule equivalence tests
# ---------------------------------------------------------------------------

from sidequest.game.ruleset.native import NativeRulesetModule  # noqa: E402

_NATIVE = NativeRulesetModule()


def test_native_slug_is_native() -> None:
    assert NativeRulesetModule.slug == "native"


@pytest.mark.parametrize("score,expected", [(10, 0), (12, 1), (8, -1), (18, 4), (3, -4), (20, 5)])
def test_native_stat_modifier_matches_legacy(score: int, expected: int) -> None:
    assert _NATIVE.stat_modifier({"STR": score}, "STR") == _stat_modifier({"STR": score}, "STR")
    assert _NATIVE.stat_modifier({"STR": score}, "STR") == expected


def test_native_stat_modifier_case_insensitive_matches_legacy() -> None:
    assert _NATIVE.stat_modifier({"str": 14}, "STR") == _stat_modifier({"str": 14}, "STR") == 2


def test_native_stat_modifier_missing_matches_legacy() -> None:
    assert _NATIVE.stat_modifier({}, "STR") == _stat_modifier({}, "STR") == 0


@pytest.mark.parametrize("base", [0, 1, 5, 10, 15])
def test_native_compute_dc_matches_legacy(base: int) -> None:
    beat = BeatDef(id="b", label="B", kind="strike", base=base, stat_check="STR")
    assert _NATIVE.compute_dc(beat) == _compute_dc(beat)
