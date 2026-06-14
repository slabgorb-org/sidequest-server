"""Structured returns from the RulesetModule chargen surface (ADR-143).

Not new global state — small typed bundles the builder applies onto the Character.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.wwn_magic import EffortPool, SpellcastingState
from sidequest.genre.models.character import ClassAbilityDef


class ChargenResources(BaseModel):
    """Effort pools + spellcasting + system strain seeded at chargen."""
    model_config = {"extra": "forbid"}
    effort: dict[str, EffortPool] = Field(default_factory=dict)
    spellcasting: SpellcastingState | None = None
    system_strain: SystemStrainPool | None = None


class FociContribution(BaseModel):
    """Skill grants + ability definitions contributed by selected Foci.

    ``abilities`` uses :class:`ClassAbilityDef` (the YAML-authored type, same as
    :class:`~sidequest.genre.models.character.FocusLevel.abilities`). The chargen
    builder (Task 10) converts these to ``AbilityDefinition``, stamping
    ``source=AbilitySource.Class`` when seeding onto ``Character.abilities``.
    """
    model_config = {"extra": "forbid"}
    skills: dict[str, int] = Field(default_factory=dict)
    abilities: list[ClassAbilityDef] = Field(default_factory=list)
