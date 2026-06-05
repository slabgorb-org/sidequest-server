"""Pure affinity-tier-resolution math for ADR-021 track 2 (affinity promotion).

Story 82-7. The progression model already declares the per-affinity ladder
(``Affinity.tier_thresholds: list[int]``) and ``AffinityState`` already carries
the live ``tier``/``progress`` pair — but nothing maps accumulated progress to a
tier. This is the mirror of the two landed siblings: ``resolve_level`` (track 1,
milestones → level) and ``resolve_wealth_tier`` (track 3, gold → wealth tier).
Track 2 needs ``resolve_affinity_tier``: accumulated progress → affinity tier.

These tests pin the *math* in the affinity's own declared units and are
deliberately agnostic about where ``progress`` comes from at runtime (that
wiring is exercised by ``tests/integration/test_affinity_tier_otel_wiring.py``).

Threshold semantics (mirrors ``resolve_wealth_tier``'s ascending-cap walk): a
character is at tier *N* = the number of authored thresholds its progress has
reached (``progress >= threshold``), clamped to the top authored tier. Tier 0 is
the floor — the affinity exists but no threshold has been crossed yet.

RED: ``resolve_affinity_tier`` does not exist yet — this module fails to import
on current ``develop``.
"""

from __future__ import annotations

import pytest

from sidequest.genre.models.progression import resolve_affinity_tier


def test_zero_progress_is_tier_zero() -> None:
    """A fresh affinity (no progress) sits at the floor, tier 0 — matching the
    ``AffinityState.tier`` default."""
    assert resolve_affinity_tier(0.0, [10, 25, 50]) == 0


def test_progress_at_first_threshold_reaches_tier_one() -> None:
    """A balance exactly on a boundary belongs to *that* tier, not the one
    below — ``progress == threshold`` counts as reached (``>=``), mirroring
    ``resolve_wealth_tier``'s inclusive cap."""
    assert resolve_affinity_tier(10.0, [10, 25, 50]) == 1


def test_progress_just_below_threshold_does_not_promote() -> None:
    """Below the threshold the affinity stays at the lower tier — the boundary
    belongs to the next tier only when fully reached."""
    assert resolve_affinity_tier(9.99, [10, 25, 50]) == 0


def test_multi_tier_crossing_in_one_resolve() -> None:
    """Accumulation that spans two thresholds at once resolves to the higher
    tier (multi-tier promotion), not a single bump. progress 30 against
    [10, 25, 50] has reached 10 and 25 → tier 2."""
    assert resolve_affinity_tier(30.0, [10, 25, 50]) == 2


def test_caps_at_top_authored_tier() -> None:
    """Progress far beyond the ceiling clamps to the top authored tier rather
    than running away — mirrors ``resolve_wealth_tier``'s over-cap clamp to the
    richest authored tier. Three thresholds → max tier 3."""
    assert resolve_affinity_tier(1000.0, [10, 25, 50]) == 3


def test_no_thresholds_is_tier_zero_without_crashing() -> None:
    """No Silent Fallbacks: an affinity whose author declared no ``tier_thresholds``
    has no ladder to climb — resolve to the floor tier 0, never fabricate a tier
    the content author didn't declare, never raise on the empty list."""
    assert resolve_affinity_tier(500.0, []) == 0


def test_negative_progress_floors_at_tier_zero() -> None:
    """A negative progress value (should never happen, but guard it) floors at
    tier 0 rather than producing a sub-floor or negative tier."""
    assert resolve_affinity_tier(-5.0, [10, 25, 50]) == 0


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (0.0, 0),
        (9.0, 0),
        (10.0, 1),
        (24.0, 1),
        (25.0, 2),
        (49.0, 2),
        (50.0, 3),
        (999.0, 3),
    ],
)
def test_threshold_table(progress: float, expected: int) -> None:
    """Tabulated boundary walk against [10, 25, 50] — every entry is a distinct
    threshold case (one below, one on, one above each boundary), not the same
    path re-run (lang-review #6: parametrized cases must exercise distinct
    paths)."""
    assert resolve_affinity_tier(progress, [10, 25, 50]) == expected
