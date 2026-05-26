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
class CheckRollParams:
    """A non-beat check (skill check or save): the dice pool, modifier, and difficulty."""
    sides: int           # 6 for 2d6 skill checks, 20 for saves
    count: int           # 2 for skill checks, 1 for saves
    modifier: int        # attr mod (+ skill level for skill checks)
    difficulty: int      # SWN difficulty (skill check) or save target
    label: str           # human label for the dice overlay context, e.g. "Notice check" / "Physical save"
