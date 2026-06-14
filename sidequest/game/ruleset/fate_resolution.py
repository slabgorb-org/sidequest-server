"""Pure Fate Core resolution primitive (ADR-144, design §4.1).

4dF (four Fudge dice, each -1/0/+1) + a skill rating on the ladder, versus an
opposition value. Shifts = ladder_total - opposition; the tier follows from
shifts. Pure and side-effect-free — the module layer (fate.py) wraps this and
emits OTEL. Used by every Fate roll: conflict AND out-of-combat overcome /
create-advantage.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum

#: The Fate ladder — adjective per integer rung.
LADDER: dict[int, str] = {
    -2: "Terrible",
    -1: "Poor",
    0: "Mediocre",
    1: "Average",
    2: "Fair",
    3: "Good",
    4: "Great",
    5: "Superb",
    6: "Fantastic",
    7: "Epic",
    8: "Legendary",
}


def ladder_name(value: int) -> str:
    """Adjective for a ladder value. Out-of-band values report the nearest
    named edge with an offset (the book encourages naming beyond Legendary)."""
    if value < -2:
        return f"Terrible{value + 2}"
    if value > 8:
        return f"Legendary+{value - 8}"
    return LADDER[value]


class FateTier(str, Enum):
    """The four Fate outcomes, by shift count."""

    Fail = "Fail"
    Tie = "Tie"
    Succeed = "Succeed"
    SucceedWithStyle = "SucceedWithStyle"


@dataclass(frozen=True)
class Opposition:
    """The number a roll must meet, plus how it arose (for OTEL and fiction).

    ``kind`` is ``"active"`` (an opponent rolled this total) or ``"passive"``
    (a set difficulty on the ladder). The math uses only ``value``.
    """

    value: int
    kind: str


@dataclass(frozen=True)
class FateOutcome:
    """One resolved Fate roll. ``dice`` are the raw 4dF faces."""

    dice: tuple[int, int, int, int]
    roll_total: int
    ladder_total: int
    opposition: int
    shifts: int
    tier: FateTier


def classify_outcome(ladder_total: int, opposition_value: int) -> tuple[int, FateTier]:
    """Shifts + tier for a ladder total vs an opposition value (pure)."""
    shifts = ladder_total - opposition_value
    if shifts < 0:
        tier = FateTier.Fail
    elif shifts == 0:
        tier = FateTier.Tie
    elif shifts <= 2:
        tier = FateTier.Succeed
    else:
        tier = FateTier.SucceedWithStyle
    return shifts, tier


def roll_4df(rng: random.Random) -> tuple[int, int, int, int]:
    """Roll four Fudge dice; each face is -1, 0, or +1."""
    return (
        rng.choice((-1, 0, 1)),
        rng.choice((-1, 0, 1)),
        rng.choice((-1, 0, 1)),
        rng.choice((-1, 0, 1)),
    )


def resolve_action(
    *,
    skill_rating: int,
    opposition: Opposition,
    rng: random.Random,
    invoke_bonus: int = 0,
) -> FateOutcome:
    """Resolve one Fate action. ``invoke_bonus`` is the net +2-per-invoke
    modifier already decided by the caller (reroll handling is a higher layer)."""
    dice = roll_4df(rng)
    roll_total = sum(dice)
    ladder_total = roll_total + skill_rating + invoke_bonus
    shifts, tier = classify_outcome(ladder_total, opposition.value)
    return FateOutcome(
        dice=dice,
        roll_total=roll_total,
        ladder_total=ladder_total,
        opposition=opposition.value,
        shifts=shifts,
        tier=tier,
    )
