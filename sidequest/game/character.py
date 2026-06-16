"""Character — unified model combining narrative identity and mechanical stats.

ADR-007: Single struct, narrative-first field ordering.

Phase 1 slice — narrator-relevant fields fully covered. Combat methods
(``is_broken``, ``edge_fraction``) live as Python methods since the
``Combatant`` Protocol is an internal concern. Fields from deferred
subsystems are included (no elision) with comments marking their phase.
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from sidequest.game.creature_core import CreatureCore
from sidequest.protocol.models import AbilityDefinition, FactCategory


class KnownFact(BaseModel):
    """A fact the character has learned — accumulates monotonically.

    P1-required: narrator uses known_facts for context.

    ``confidence`` is a closed ``Literal`` — one of ``"Certain"``,
    ``"Suspected"``, ``"Rumored"``, ``"Discovered"`` (default ``"Certain"``).
    The pre-50-17 legacy value ``"confirmed"`` is rejected. The literal
    mirrors the canonical set in
    :data:`sidequest.server.dispatch.scenario_accusation._SUPPORTED_CONFIDENCES`
    and the type on :class:`sidequest.game.accusation.AccusationItem`.
    ``ScenarioClueIntake`` mints ``"Discovered"``; narrator-emitted facts
    via ``WorldStatePatch.discovered_facts`` use the default unless they
    override. Unknown keys on the model are rejected (``extra="forbid"``).
    """

    model_config = {"extra": "forbid"}

    content: str
    confidence: Literal["Certain", "Suspected", "Rumored", "Discovered"] = "Certain"
    # P5-deferred: source/learned_turn used by scenario system
    source: str = "GameEvent"
    learned_turn: int = 0
    # 50-14: stable identifier for UI dedup (ADR-100 Seam C); auto-minted
    # when not supplied so legacy fact creation sites keep working. Scenario
    # clue intake passes the clue id through here.
    fact_id: str = Field(default_factory=lambda: uuid4().hex)
    category: FactCategory = FactCategory.Lore


class AffinityState(BaseModel):
    """Per-affinity tier tracking for ability progression.

    P6-deferred: advancement/affinity progression, not needed for
    narration.
    """

    model_config = {"extra": "forbid"}

    affinity_id: str
    tier: int = 0
    progress: float = 0.0


class Character(BaseModel):
    """A player character with unified narrative + mechanical identity.

    CreatureCore is nested under ``core`` internally — callers access
    fields via ``character.core.name`` etc. For narrator prompt building,
    access ``character.core.name``.

    P1-required fields: core, backstory, narrative_state, hooks, char_class,
                        race, pronouns, stats, abilities, known_facts,
                        is_friendly.
    P6-deferred fields: affinities (affinity progression).
    P2-deferred fields: resolved_archetype, archetype_provenance (chargen axis).
    """

    model_config = {"extra": "forbid"}

    # Embedded CreatureCore — callers use character.core.name, etc.
    core: CreatureCore

    # Narrative identity (P1-required)
    backstory: str
    narrative_state: str = ""
    hooks: list[str] = Field(default_factory=list)

    # Mechanical identity (P1-required)
    char_class: str
    race: str
    pronouns: str = ""
    stats: dict[str, int] = Field(default_factory=dict)

    # Abilities (P1-required — narrator uses genre_description for context)
    abilities: list[AbilityDefinition] = Field(default_factory=list)

    # Character knowledge (P1-required — narrator uses known_facts for continuity)
    known_facts: list[KnownFact] = Field(default_factory=list)

    # P6-deferred: affinity tier progression (advancement system, Epic F8)
    affinities: list[AffinityState] = Field(default_factory=list)

    # P1-required: determines narrator behaviour (player vs enemy framing)
    is_friendly: bool = True

    # P2-deferred: archetype resolution (chargen axis system, story G2)
    resolved_archetype: str | None = None
    archetype_provenance: dict | None = None

    # Chargen-derived narrative identity (canned-openings P2 — used by
    # _populate_opening_directive_on_chargen_complete to filter Openings
    # by triggers.backgrounds and to render PC name forms in the
    # chassis-voice block).
    background: str = ""
    drive: str = ""
    first_name: str = ""
    last_name: str = ""
    nickname: str = ""

    # Position on the chassis interior (narrator-tracked, optional).
    # Set via state_patch when the narrator moves a character between
    # rooms; rendered on the Ship tab. Stays None until the narrator
    # sets it; the renderer falls back to a chassis-default room.
    current_room: str | None = None

    @field_validator("backstory")
    @classmethod
    def backstory_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("backstory cannot be blank")
        return v

    @field_validator("char_class")
    @classmethod
    def char_class_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("char_class cannot be blank")
        return v

    @field_validator("race")
    @classmethod
    def race_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("race cannot be blank")
        return v

    # ------------------------------------------------------------------
    # Combatant Protocol implementation
    # ------------------------------------------------------------------

    def name(self) -> str:
        """Character display name."""
        return self.core.name

    def hp(self) -> int:
        """Current HP value (ADR-114)."""
        return self.core.hp.current

    def max_hp(self) -> int:
        """Maximum HP value (ADR-114)."""
        return self.core.hp.max

    def level(self) -> int:
        """Character level."""
        return self.core.level

    def is_broken(self) -> bool:
        """True when HP is at or below zero (combatant is down).

        ``self.hp() <= 0`` — negative HP counts as broken.
        """
        return self.core.hp.current <= 0

    def hp_fraction(self) -> float:
        """HP fraction as float in [0.0, 1.0].

        Returns ``0.0`` when ``max_hp == 0`` (NOT ``1.0``, NOT
        ``ZeroDivisionError``).
        """
        if self.core.hp.max == 0:
            return 0.0
        return self.core.hp.current / self.core.hp.max
