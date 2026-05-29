"""WWN magic — per-CreatureCore data models (pure data; rules live in WwnRulesetModule).

Mirrors game/system_strain.py: the model is inert; gating, reclaim, and span
emission are owned by WwnRulesetModule. Faithful to WWN SRD §1.4.4 (Effort:
per-source commitment pools with maintained/scene/day durations) and §4.2
(spells: a prepared list + a daily 'casts' pool + a max castable level — NOT
per-level Vancian slots).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EffortDuration = Literal["maintained", "scene", "day"]
VeteransLuckMode = Literal["force_hit", "force_miss"]


class EffortCommitment(BaseModel):
    model_config = {"extra": "forbid"}

    points: int
    duration: EffortDuration
    label: str = ""           # the Art/power the Effort fuels, for the GM panel


class EffortPool(BaseModel):
    """One Effort pool for ONE class-source (High Mage, Vowed, ...). Points from
    one source cannot fuel another (SRD §1.4.4), so a caster carries a dict of
    these keyed by source. ``max`` is seeded at chargen = effort_base + relevant
    skill level + governing attribute modifier (Partial class: -1, min 1)."""

    model_config = {"extra": "forbid"}

    source: str
    max: int
    commitments: list[EffortCommitment] = Field(default_factory=list)

    @property
    def committed(self) -> int:
        return sum(c.points for c in self.commitments)

    @property
    def available(self) -> int:
        return self.max - self.committed


class SpellcastingState(BaseModel):
    """WWN spell economy (SRD §4.2). A cast spends ONE from ``casts_remaining``
    on ANY prepared spell of level <= max_spell_level; refreshes to casts_per_day
    on a night's rest. ``prepared`` holds spell ids chosen at rest from the
    spellbook (content)."""

    model_config = {"extra": "forbid"}

    prepared: list[str] = Field(default_factory=list)
    casts_remaining: int = 0
    casts_per_day: int = 0
    max_spell_level: int = 0


class EffortResult(BaseModel):
    model_config = {"extra": "forbid"}
    applied: bool
    source: str
    available: int
    max: int
    reason: str = ""


class SpellcastResult(BaseModel):
    model_config = {"extra": "forbid"}
    cast: bool
    spell_id: str
    casts_remaining: int
    save_made: bool | None = None
    damage: int = 0
    reason: str = ""


class VeteransLuckResult(BaseModel):
    model_config = {"extra": "forbid"}
    applied: bool
    mode: VeteransLuckMode   # "force_hit" | "force_miss"
    reason: str = ""
