"""Trope definition types from tropes.yaml.

Port of sidequest-genre/src/models/tropes.rs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TropeEscalation(BaseModel):
    """A single escalation step within a trope.

    ``roles`` targets which player archetype/role this escalation beat
    applies to (elemental_harmony uses ``the-one-who-sacrifices`` for
    guest-NPC multiplayer routing). Rust silently dropped it; accepted here
    as pass-through until a consumer wires it.
    """

    model_config = {"extra": "forbid"}

    at: float
    event: str
    npcs_involved: list[str] = Field(default_factory=list)
    stakes: str = ""
    roles: list[str] = Field(default_factory=list)


class PassiveProgression(BaseModel):
    """Passive progression configuration for a trope."""

    model_config = {"extra": "forbid"}

    rate_per_turn: float = 0.0
    rate_per_day: float = 0.0
    accelerators: list[str] = Field(default_factory=list)
    decelerators: list[str] = Field(default_factory=list)
    accelerator_bonus: float = 0.0
    decelerator_penalty: float = 0.0


class TropeDefinition(BaseModel):
    """A narrative trope definition (genre-level or world-level)."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    id: str | None = None
    name: str
    description: str | None = None
    category: str = ""
    triggers: list[str] = Field(default_factory=list)
    narrative_hints: list[str] = Field(default_factory=list)
    tension_level: float | None = None
    resolution_hints: list[str] | None = None
    resolution_patterns: list[str] | None = None
    tags: list[str] = Field(default_factory=list)
    escalation: list[TropeEscalation] = Field(default_factory=list)
    passive_progression: PassiveProgression | None = None
    is_abstract: bool = Field(default=False, alias="abstract")
    extends: str | None = None


class SeedTrope(BaseModel):
    """A short-arc 'seed' trope dealt from a per-pack deck (Epic 22).

    Sibling to :class:`TropeDefinition`, not a field extension: seeds have a
    different lifecycle (short-arc + deck draw + ghost retention). Deliberately
    vague — the narrator retroactively connects an active seed to whatever
    macro-trope escalation emerges. ``extra="forbid"`` mirrors TropeDefinition
    so authoring typos in seed YAML fail loudly.
    """

    model_config = {"extra": "forbid", "populate_by_name": True}

    id: str
    name: str
    description: str | None = None
    flavor_tags: list[str] = Field(default_factory=list)
    lifespan_turns: int = 0
    delivery_hints: list[str] = Field(default_factory=list)
    narrative_hint: str = ""
