"""Structured returns from the RulesetModule chargen surface (ADR-143).

Not new global state — small typed bundles the builder applies onto the Character.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.wwn_magic import EffortPool, SpellcastingState


class ChargenResources(BaseModel):
    """Effort pools + spellcasting + system strain seeded at chargen."""
    model_config = {"extra": "forbid"}
    effort: dict[str, EffortPool] = Field(default_factory=dict)
    spellcasting: SpellcastingState | None = None
    system_strain: SystemStrainPool | None = None


class FociContribution(BaseModel):
    """Skill grants + ability definitions contributed by selected Foci."""
    model_config = {"extra": "forbid"}
    skills: dict[str, int] = Field(default_factory=dict)
    abilities: list = Field(default_factory=list)
