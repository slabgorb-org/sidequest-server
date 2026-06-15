"""Narrative support types: prompts, openings, beat vocabulary, achievements, power tiers.

Port of sidequest-genre/src/models/narrative.rs.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from sidequest.genre.models.chassis import BondTier


class Prompts(BaseModel):
    """LLM prompt templates for different agent roles.

    Genre-specific prompts (``ritual``, ``debt_collection``,
    ``session_opener_template``, ``scene_description``) are authored
    per-pack and accepted here as pass-through. Consumers should look
    them up by key when a genre context triggers the corresponding
    scene. ``scene_description`` is the tea_and_murder pack's gothic
    establishing-shot prompt ("Describe locations through... before
    anyone speaks") — added when its absence blocked the entire
    Brontë pack from loading (playtest 2026-04-26, Sonia's session).
    """

    model_config = {"extra": "forbid"}

    narrator: str
    combat: str
    npc: str
    world_state: str
    chase: str | None = None
    transition_hints: dict[str, str] = Field(default_factory=dict)
    extraction: str | None = None
    keeper_monologue: str | None = None
    town: str | None = None
    chargen: str | None = None
    ritual: str | None = None
    debt_collection: str | None = None
    session_opener_template: str | None = None
    scene_description: str | None = None


class BeatObstacle(BaseModel):
    """A chase obstacle."""

    model_config = {"extra": "forbid"}

    name: str
    description: str
    stat_check: str
    failure_penalty: str
    tags: list[str] = Field(default_factory=list)


class BeatVocabulary(BaseModel):
    """Chase/beat vocabulary configuration.

    heavy_metal authored ``event_flavor``, ``decision_framings``, and
    ``chase_modes`` as additional prose/pacing hooks. Rust silently dropped
    them; accepted here as pass-through.
    """

    model_config = {"extra": "forbid"}

    obstacles: list[BeatObstacle] = Field(default_factory=list)
    event_flavor: list[dict[str, Any]] = Field(default_factory=list)
    decision_framings: list[str] = Field(default_factory=list)
    chase_modes: list[str] = Field(default_factory=list)


class Achievement(BaseModel):
    """An achievement linked to trope progression."""

    model_config = {"extra": "forbid"}

    id: str
    name: str
    description: str
    trope_id: str
    trigger_status: str
    emoji: str


class PowerTier(BaseModel):
    """A power tier description for a character class at a level range."""

    model_config = {"extra": "forbid"}

    level_range: list[int]
    label: str
    player: str
    npc: str | None = None


# === New unified Opening sub-models (Phase 1) ===

OpeningMode = Literal["solo", "multiplayer", "either"]


class OpeningTrigger(BaseModel):
    """Selection rules — how the bank picks this Opening at chargen-complete."""

    model_config = {"extra": "forbid"}

    mode: OpeningMode = "either"
    min_players: int = 1
    max_players: int = 6
    backgrounds: list[str] = Field(default_factory=list)


class QuestSeed(BaseModel):
    """Machine-readable quest behind an authored hook (ADR-146 §1).

    The structured form of ``tone.complication``: when the player accepts this
    offer, the engine mints a ``QuestEntry`` from these fields deterministically
    (no narrator tool call required). ``quest_id``/``title``/``objective`` map
    1:1 onto ``QuestEntry`` (session.py) and ``RecordQuestArgs`` — minting is a
    straight copy, not a re-derivation. ``stakes`` feeds ``active_stakes``
    (fill-don't-clobber); ``anchor`` flows into ``quest_anchors``; ``giver`` is
    the role naming the job so the router's acceptance detection has context for
    *which* offer a "yes" answers.

    ``extra="forbid"``: a typo'd sub-field fails loud at world load (the whole
    point of making the hook a typed field rather than a free-form passthrough).
    """

    model_config = {"extra": "forbid"}

    quest_id: str
    title: str
    objective: str
    stakes: str = ""
    anchor: str | None = None
    giver: str = ""


class OpeningTone(BaseModel):
    model_config = {"extra": "forbid"}

    register: str = ""
    stakes: str = ""
    complication: str = ""
    sensory_layers: dict[str, str] = Field(default_factory=dict)
    avoid_at_all_costs: list[str] = Field(default_factory=list)
    # ADR-146 §1: the structured quest behind the authored hook. Optional —
    # authoring a seed is opt-in ("Neutral"); an opening with no quest_seed
    # behaves exactly as today (narrator-discretion minting). Typed (not a
    # passthrough) so a misspelled sub-field fails loud at world load, and the
    # ``extra="forbid"`` above rejects a misspelled ``quest_seed`` key too.
    quest_seed: QuestSeed | None = None


_PER_PC_BEAT_KEYS = frozenset({"background", "drive", "race", "class"})


class PerPcBeat(BaseModel):
    """Chargen-keyed textural moment. Validator 6 constrains applies_to keys."""

    model_config = {"extra": "forbid"}

    applies_to: dict[str, str]
    beat: str

    @field_validator("applies_to")
    @classmethod
    def _validate_keys(cls, v: dict[str, str]) -> dict[str, str]:
        invalid = set(v.keys()) - _PER_PC_BEAT_KEYS
        if invalid:
            raise ValueError(
                f"PerPcBeat.applies_to keys must be in {sorted(_PER_PC_BEAT_KEYS)}; "
                f"got disallowed keys: {sorted(invalid)}"
            )
        return v


class SoftHook(BaseModel):
    """Pull-not-push wrinkle that surfaces when conversation lulls."""

    model_config = {"extra": "forbid"}

    kind: str = "pull_not_push"
    timing: str = "surfaces if conversation lulls; otherwise wait for turn 2"
    narration: str = ""
    escalation_path: dict[str, str] = Field(default_factory=dict)


class PartyFraming(BaseModel):
    """MP-only. Omitted from directive when mode == solo."""

    model_config = {"extra": "forbid"}

    already_a_crew: bool = False
    bond_tier_default: BondTier = "trusted"
    shared_history_seeds: list[str] = Field(default_factory=list)
    narrator_guidance: str = ""


class MagicMicrobleed(BaseModel):
    """Optional — Reach-bleeds-through detail at intensity 0.25."""

    model_config = {"extra": "forbid"}

    detail: str
    cost_bar: str | None = None


class OpeningSetting(BaseModel):
    """Either ship-anchored (Coyote Star) OR location-anchored (Aureate). Exactly one."""

    model_config = {"extra": "forbid"}

    chassis_instance: str | None = None
    interior_room: str | None = None
    location_label: str | None = None
    region_id: str | None = None
    situation: str = ""
    present_npcs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_anchor(self) -> Self:
        ship = self.chassis_instance is not None
        place = self.location_label is not None
        if ship == place:
            raise ValueError(
                "OpeningSetting must specify exactly one of "
                "chassis_instance (with interior_room) OR location_label"
            )
        if ship and not self.interior_room:
            raise ValueError("interior_room required when chassis_instance is set")
        if ship and self.present_npcs:
            raise ValueError(
                "present_npcs must be empty for chassis-anchored openings; "
                "use chassis_instance.crew_npcs instead"
            )
        return self


class Opening(BaseModel):
    """Unified opening scenario — replaces OpeningHook (solo, sketch) and
    MpOpening (MP, prose). One file per world: ``worlds/{slug}/openings.yaml``.

    Top-level model uses ``extra='allow'`` so world authors can add
    experimental fields without schema migrations. Inner sub-models
    use ``extra='forbid'`` to catch typos in well-defined fields.
    """

    model_config = {"extra": "allow"}

    id: str
    name: str = ""
    triggers: OpeningTrigger
    setting: OpeningSetting
    tone: OpeningTone = Field(default_factory=OpeningTone)
    establishing_narration: str
    first_turn_invitation: str = ""
    rig_voice_seeds: list[dict[str, Any]] = Field(default_factory=list)
    per_pc_beats: list[PerPcBeat] = Field(default_factory=list)
    soft_hook: SoftHook = Field(default_factory=SoftHook)
    party_framing: PartyFraming | None = None
    magic_microbleed: MagicMicrobleed | None = None

    @field_validator("first_turn_invitation")
    @classmethod
    def _no_question(cls, v: str) -> str:
        if "?" in v:
            raise ValueError(
                "first_turn_invitation must not contain '?'. "
                "Per SOUL pacing rule, turn 1 closes on a declarative; "
                "the player should be able to sit in the breath without prompt."
            )
        return v

    @field_validator("establishing_narration", "first_turn_invitation")
    @classmethod
    def _no_placeholder_text(cls, v: str) -> str:
        forbidden = ["[authored", "[tbd", "[migrated", "[placeholder"]
        lower = v.lower()
        for marker in forbidden:
            if marker in lower:
                raise ValueError(
                    f"Field contains placeholder marker {marker!r} — "
                    "world-builder pass not complete"
                )
        return v
