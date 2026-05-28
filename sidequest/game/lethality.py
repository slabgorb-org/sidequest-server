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

    base_total: int  # damage rolled before Trauma
    final_total: int  # damage after Trauma multiplication (== base_total if not traumatic)
    traumatic: bool  # did the Trauma Die meet/exceed the Trauma Target?
    trauma_roll: int  # the Trauma Die result (0 if the weapon has no trauma_die)
    trauma_target: int  # the target it was rolled against


class DownedResult(BaseModel):
    """Outcome of resolving a character dropped to 0 HP under CWN."""

    model_config = {"extra": "forbid"}

    mortal: bool  # Mortal Injury declared (always True under CWN at 0 HP from lethal damage)
    major: bool  # Major Injury table rolled (only when a Traumatic Hit landed this scene)
    major_roll: int  # 1d12 result (0 if no Major Injury roll)
    major_text: str  # the table entry text ("" if no roll)
    save_made: bool  # Physical save result (True = save succeeded, no Major Injury)


# CWN Major Injury table (1d12). Rolled when a character drops to 0 HP in a
# scene where a Traumatic Hit landed and they failed the Physical save. Text is
# the in-world consequence the narrator dramatizes; the engine applies it as a
# Scar-severity Status (see CwnRulesetModule.resolve_downed).
MAJOR_INJURY_TABLE: dict[int, str] = {
    1: "Knocked senseless — out cold for the rest of the scene, but no lasting harm.",
    2: "Deep bleeding wound. Frail until properly treated and rested.",
    3: "Broken limb — an arm or leg is fractured; that limb is useless until set and healed.",
    4: "Cracked ribs and internal bruising. Every exertion costs; Frail until healed.",
    5: "Severe blood loss. Stabilize within the hour or slip toward death.",
    6: "Concussion — dazed, disoriented; mental tasks suffer until recovered.",
    7: "Lost an eye. Permanent — depth perception and ranged accuracy are diminished.",
    8: "Mangled hand — fingers crushed or severed; fine manipulation is permanently impaired.",
    9: "Severed limb — an arm or leg is lost. Permanent without expensive chrome.",
    10: "Internal damage — a punctured organ. Frail and failing until major surgery.",
    11: "Brain damage — permanent cognitive or motor impairment.",
    12: "Instant death. The wound is mortal beyond saving.",
}


def major_injury_entry(roll: int) -> str:
    """Return the Major Injury text for a 1d12 roll. Fail loud out of range."""
    if roll not in MAJOR_INJURY_TABLE:
        raise ValueError(f"major injury roll {roll} out of range 1..12")
    return MAJOR_INJURY_TABLE[roll]
