"""Value types for the generalized RulesetModule resolution surface.

A module computes these from the full turn context (attacker + target), so SWN can
read target AC where native reads a beat DC. Frozen — pure data, no behavior.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttackRollParams:
    """Everything dispatch needs to roll one attack: the d20 modifier and the number to meet."""
    modifier: int        # attacker to-hit modifier (native: stat mod; SWN: attack_bonus + skill + attr)
    target_number: int   # number the roll must meet/beat (native: beat DC; SWN: target AC)


@dataclass(frozen=True)
class OpponentAttackOutcome:
    """Result of one server-driven enemy attack (the SWN beat_selection enemy turn).

    The opponent rolls d20 + modifier vs the player's AC; ``hit`` is the verdict.
    Carries the full to-hit math so the GM-panel lie-detector can audit the
    reprisal (playtest perseus_cloud: hp_depletion combat had no enemy turn, so
    the player could never lose)."""

    hit: bool
    attack_total: int  # d20 + modifier
    modifier: int  # attack_bonus + combat_skill + attribute mod
    d20: int
    target_ac: int


@dataclass(frozen=True)
class CheckRollParams:
    """A non-beat check (skill check or save): the dice pool, modifier, and difficulty."""
    sides: int           # 6 for 2d6 skill checks, 20 for saves
    count: int           # 2 for skill checks, 1 for saves
    modifier: int        # attr mod (+ skill level for skill checks)
    difficulty: int      # SWN difficulty (skill check) or save target
    label: str           # human label for the dice overlay context, e.g. "Notice check" / "Physical save"
