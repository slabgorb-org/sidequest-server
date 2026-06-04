"""Pure level-resolution math for ADR-021 track 1 (milestone → level-up).

Story 82-6. The progression model already declares the milestone units
(``milestones_per_level``, ``max_level``) and ships a sibling resolver for
track 3 (``resolve_wealth_tier``: gold → wealth tier). Track 1 needs the
mirror function: accumulated milestones → character level.

These tests pin the *math* in the config's own declared units and are
deliberately agnostic about where milestones come from at runtime (that
wiring is exercised by ``tests/integration/test_levelup_otel_wiring.py``).

RED: ``resolve_level`` does not exist yet — this module fails to import on
current ``develop``.
"""

from __future__ import annotations

import pytest

from sidequest.genre.models.progression import ProgressionConfig, resolve_level


def _config(*, per_level: int, max_level: int) -> ProgressionConfig:
    return ProgressionConfig(
        milestone_categories=["combat", "exploration"],
        milestones_per_level=per_level,
        max_level=max_level,
    )


def test_zero_milestones_is_level_one():
    """A fresh character (no milestones) sits at the floor, level 1 —
    matching ``CreatureCore.level`` default."""
    assert resolve_level(0, _config(per_level=3, max_level=8)) == 1


def test_one_full_bucket_levels_to_two():
    """``milestones_per_level`` accumulated milestones crosses exactly one
    threshold: level 1 → 2."""
    assert resolve_level(3, _config(per_level=3, max_level=8)) == 2


def test_partial_bucket_does_not_level():
    """Below the threshold the character stays at the lower level — the
    boundary belongs to the *next* level only when fully reached."""
    assert resolve_level(2, _config(per_level=3, max_level=8)) == 1


def test_multi_level_crossing_in_one_resolve():
    """AC1 edge: accumulation that spans two thresholds at once resolves to
    the higher level (multi-level-up), not a single bump. 7 milestones at
    3/level → 1 + 7//3 = level 3."""
    assert resolve_level(7, _config(per_level=3, max_level=8)) == 3


def test_caps_at_max_level():
    """AC1 edge: accumulation far beyond the ceiling clamps to ``max_level``
    rather than running away — mirrors ``resolve_wealth_tier``'s over-cap
    clamp to the richest authored tier."""
    assert resolve_level(1000, _config(per_level=3, max_level=5)) == 5


def test_unconfigured_progression_is_level_one_without_crashing():
    """No Silent Fallbacks / safety: a pack that doesn't author progression
    (``milestones_per_level == 0`` default — e.g. caverns_and_claudes) must
    resolve to the floor level, never raise ``ZeroDivisionError``."""
    assert resolve_level(50, _config(per_level=0, max_level=0)) == 1


def test_negative_milestones_floor_at_level_one():
    """A negative accumulation (should never happen, but guard it) floors at
    level 1 rather than producing a sub-floor or negative level."""
    assert resolve_level(-5, _config(per_level=3, max_level=8)) == 1


@pytest.mark.parametrize(
    ("completed", "expected"),
    [(0, 1), (1, 1), (2, 1), (3, 2), (5, 2), (6, 3), (9, 4)],
)
def test_threshold_table_per_level_three(completed: int, expected: int):
    """Tabulated boundary walk at 3 milestones/level, max 8 — every entry is
    a distinct threshold case, not the same path re-run (lang-review #6)."""
    assert resolve_level(completed, _config(per_level=3, max_level=8)) == expected
