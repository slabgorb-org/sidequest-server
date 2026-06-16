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

    # current/permanent are float: CWN cyberware costs fractional System Strain
    # (0.25/0.5), so accumulated totals can be fractional (Keith's ruling,
    # 2026-06-14; see CatalogItem.system_strain). ``max`` is the CON-derived
    # ceiling and stays an integer.
    current: float = 0
    max: int
    permanent: float = 0


class StrainResult(BaseModel):
    """Outcome of an attempted strain change, for the narrator/tool to describe."""

    model_config = {"extra": "forbid"}

    applied: bool
    current: float
    max: int
    permanent: float
    delta: float
    reason: str = ""
