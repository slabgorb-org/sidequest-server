"""CWN combat-lethality data models + Major Injury table.

Pure data + a lookup table. All rules (Trauma roll, Shock, Mortal/Major Injury
resolution) live in CwnRulesetModule. Mirrors plan 2's system_strain.py split:
dumb models here, behavior in the ruleset module.
"""

from __future__ import annotations

from pydantic import BaseModel


class LethalityResult(BaseModel):
    """Outcome of a Trauma check on a hit (for the narrator/dispatch to apply)."""

    model_config = {"extra": "forbid"}

    base_total: int       # damage rolled before Trauma
    final_total: int      # damage after Trauma multiplication (== base_total if not traumatic)
    traumatic: bool       # did the Trauma Die meet/exceed the Trauma Target?
    trauma_roll: int      # the Trauma Die result (0 if the weapon has no trauma_die)
    trauma_target: int    # the target it was rolled against


class DownedResult(BaseModel):
    """Outcome of resolving a character dropped to 0 HP under CWN."""

    model_config = {"extra": "forbid"}

    mortal: bool          # Mortal Injury declared (always True under CWN at 0 HP from lethal damage)
    major: bool           # Major Injury table rolled (only when a Traumatic Hit landed this scene)
    major_roll: int       # 1d12 result (0 if no Major Injury roll)
    major_text: str       # the table entry text ("" if no roll)
    save_made: bool       # Physical save result (True = save succeeded, no Major Injury)
