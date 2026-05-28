"""System Strain — CWN's CON-bound chrome-cost resource.

A real engine-tracked pool on CreatureCore (mirrors the ablative HpPool):
- ``max`` == the character's CONSTITUTION-flavor (Body) score, seeded at chargen.
- ``current`` starts at 0 and rises as cyber is installed/activated, drugs are
  taken, and first aid is applied. An add that would push ``current`` past
  ``max`` is REFUSED by the rules layer (CwnRulesetModule.apply_system_strain).
- ``permanent`` is the floor set by installed cyberware: rest recovers
  ``current`` down to ``permanent``, never below.

This model is pure data. All rules (gating, permanent floor, rest recovery,
first-aid cost) live in CwnRulesetModule.apply_system_strain.
"""

from __future__ import annotations

from pydantic import BaseModel


class SystemStrainPool(BaseModel):
    model_config = {"extra": "forbid"}

    current: int = 0
    max: int
    permanent: int = 0


class StrainResult(BaseModel):
    """Outcome of an attempted strain change, for the narrator/tool to describe."""

    model_config = {"extra": "forbid"}

    applied: bool
    current: int
    max: int
    permanent: int
    delta: int
    reason: str = ""
