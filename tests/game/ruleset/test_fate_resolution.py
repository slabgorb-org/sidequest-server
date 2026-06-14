from __future__ import annotations

import random

import pytest

from sidequest.game.ruleset.fate_resolution import (
    FateOutcome,
    FateTier,
    Opposition,
    classify_outcome,
    ladder_name,
    resolve_action,
    roll_4df,
)


@pytest.mark.parametrize(
    "ladder_total,opposition,expected_shifts,expected_tier",
    [
        (3, 5, -2, FateTier.Fail),  # rolled under
        (5, 5, 0, FateTier.Tie),  # exactly met
        (6, 5, 1, FateTier.Succeed),  # +1 shift
        (7, 5, 2, FateTier.Succeed),  # +2 shifts
        (8, 5, 3, FateTier.SucceedWithStyle),  # +3 shifts
        (12, 5, 7, FateTier.SucceedWithStyle),
    ],
)
def test_classify_outcome(ladder_total, opposition, expected_shifts, expected_tier):
    shifts, tier = classify_outcome(ladder_total, opposition)
    assert shifts == expected_shifts
    assert tier is expected_tier


def test_roll_4df_is_four_fudge_dice():
    rng = random.Random(12345)
    for _ in range(200):
        dice = roll_4df(rng)
        assert len(dice) == 4
        assert all(d in (-1, 0, 1) for d in dice)
    # range of the sum is [-4, 4]
    sums = [sum(roll_4df(rng)) for _ in range(500)]
    assert min(sums) >= -4 and max(sums) <= 4


def test_resolve_action_invariants():
    rng = random.Random(7)
    opp = Opposition(value=2, kind="passive")
    outcome = resolve_action(skill_rating=3, opposition=opp, rng=rng, invoke_bonus=2)
    assert isinstance(outcome, FateOutcome)
    assert outcome.roll_total == sum(outcome.dice)
    assert outcome.ladder_total == outcome.roll_total + 3 + 2
    assert outcome.shifts == outcome.ladder_total - 2
    expected_shifts, expected_tier = classify_outcome(outcome.ladder_total, 2)
    assert outcome.shifts == expected_shifts
    assert outcome.tier is expected_tier


def test_ladder_name_known_and_out_of_band():
    assert ladder_name(0) == "Mediocre"
    assert ladder_name(4) == "Great"
    assert ladder_name(8) == "Legendary"
    assert ladder_name(9).startswith("Legendary")
    assert ladder_name(-3).startswith("Terrible")
