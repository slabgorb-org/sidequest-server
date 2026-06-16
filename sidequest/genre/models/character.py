"""Character-related types: archetypes, creation scenes, visual style.

Port of sidequest-genre/src/models/character.rs.
"""

from __future__ import annotations

import random as _random
import re as _re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from sidequest.genre.models.ocean import OceanProfile

if TYPE_CHECKING:
    from sidequest.genre.models.rules import SavingThrowsTable


class NpcArchetype(BaseModel):
    """An NPC archetype template.

    No extra="forbid" — genre packs may add genre-specific fields (role, morale, etc.)
    that are not in the base struct. Rust serde silently ignores unknown fields here.
    """

    model_config = {"extra": "allow"}

    name: str
    description: str
    personality_traits: list[str] = Field(default_factory=list)
    typical_classes: list[str] = Field(default_factory=list)
    typical_races: list[str] = Field(default_factory=list)
    stat_ranges: dict[str, list[int]] = Field(default_factory=dict)
    inventory_hints: list[str] = Field(default_factory=list)
    dialogue_quirks: list[str] = Field(default_factory=list)
    disposition_default: int = 0
    catalog_items: list[str] = Field(default_factory=list)
    ocean: OceanProfile | None = None
    mindless: bool = False
    saves_as_class: str = "Fighter"


class IdentityCapture(BaseModel):
    """Story-scene identity capture flags (pronouns + freeform fields).

    Used by the_story scene in genre packs that fold pronouns into a
    combined identity scene.
    """

    model_config = {"extra": "forbid"}

    pronouns_required: bool = True
    background_optional: bool = True
    description_optional: bool = True


class MechanicalEffects(BaseModel):
    """Mechanical effects of a character creation choice or scene-level directive."""

    model_config = {"extra": "forbid"}

    class_hint: str | None = None
    race_hint: str | None = None
    mutation_hint: str | None = None
    item_hint: str | None = None
    affinity_hint: str | None = None
    training_hint: str | None = None
    background: str | None = None
    personality_trait: str | None = None
    emotional_state: str | None = None
    relationship: str | None = None
    goals: str | None = None
    allows_freeform: bool | None = None
    rig_type_hint: str | None = None
    rig_trait: str | None = None
    catch_phrase: str | None = Field(default=None, alias="catch", serialization_alias="catch")
    stat_bonuses: dict[str, int] = Field(default_factory=dict)
    pronoun_hint: str | None = None
    stat_generation: str | None = None
    equipment_generation: str | None = None
    jungian_hint: str | None = None
    rpg_role_hint: str | None = None
    # spaghetti_western: chargen-choice-applied reputation tag
    # (e.g. "intimidation", "stealth", "network"). Rust dropped it;
    # reputation system unwired. Pass-through.
    reputation_bonus: str | None = None

    # Arrange-scene flags (the_arrangement)
    assignment_required: bool | None = None
    allow_reject: bool | None = None

    # Story-scene flags (the_story)
    identity_capture: IdentityCapture | None = None
    background_autogen_source: str | None = None

    model_config = {"extra": "forbid", "populate_by_name": True}


class ClassMagicConfig(BaseModel):
    """Per-class magic configuration. Loaded from classes.yaml.

    Carried into MagicState at chargen by magic_init to instantiate
    per-actor known/prepared/slot bookkeeping.
    """

    model_config = {"extra": "forbid"}

    tradition: str  # "arcane" | "divine"
    # str-keyed dicts because YAML 1.1 + JSON serialization both flatten
    # int keys to strings; pydantic handles round-trip.
    slots_by_class_level: dict[str, dict[str, int]]
    starting_known_spells: int
    save_dc_stat: str  # "INT" | "WIS" | "CHA"
    turn_undead: bool = False  # cleric-only class-special


class ClassAbilityDef(BaseModel):
    """Class-source signature ability authored in classes.yaml.

    Mirrors AbilityDefinition (sidequest.game.character) minus the
    `source` field. Loader stamps source=AbilitySource.Class on each
    entry during chargen seeding so authors don't have to type a
    discriminator they never vary.

    Spec: docs/superpowers/specs/2026-05-10-class-mechanical-surface-design.md §5.2.
    """

    model_config = {"extra": "forbid"}

    name: str
    genre_description: str
    mechanical_effect: str
    involuntary: bool = False

    @field_validator("name", "genre_description", "mechanical_effect")
    @classmethod
    def _non_blank(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"ClassAbilityDef field {info.field_name!r} must be non-blank")
        return v


class ClassDef(BaseModel):
    """A character class definition loaded from classes.yaml.

    Class influences starting Edge (via edge_config.base_max_by_class
    in rules.yaml), starting equipment kit, and (when magic_access is
    set) per-class magic config consumed by the magic_init pipeline.
    """

    model_config = {"extra": "forbid"}

    id: str
    display_name: str
    rpg_role: str
    jungian_default: str
    prime_requisite: str  # "STR" / "DEX" / "CON" / "INT" / "WIS" / "CHA"
    minimum_score: int
    kit_table: str
    flavor: str = ""
    encounter_beat_choices: list[str] = Field(default_factory=list)
    abilities: list[ClassAbilityDef] = Field(default_factory=list)
    magic_access: str | None = None
    magic_config: ClassMagicConfig | None = None
    saving_throws: SavingThrowsTable | None = None


class CharCreationChoice(BaseModel):
    """A choice within a character creation scene."""

    model_config = {"extra": "forbid"}

    label: str
    description: str
    mechanical_effects: MechanicalEffects


class CharCreationScene(BaseModel):
    """A character creation scene with narrative choices."""

    model_config = {"extra": "forbid"}

    id: str
    title: str
    narration: str
    choices: list[CharCreationChoice] = Field(default_factory=list)
    loading_text: str | None = None
    allows_freeform: bool | None = None
    hook_prompt: str | None = None
    mechanical_effects: MechanicalEffects | None = None


class BackstoryTables(BaseModel):
    """Random backstory composition tables loaded from backstory_tables.yaml."""

    # No deny_unknown_fields — deserializer extracts template + dynamic table keys
    template: str
    tables: dict[str, list[str]] = Field(default_factory=dict)

    @classmethod
    def model_validate(cls, obj: object, **kwargs: Any) -> BackstoryTables:  # type: ignore[override]
        """Extract template and remaining string-list keys as tables."""
        if isinstance(obj, dict):
            data: dict[str, Any] = dict(obj)
            template = data.get("template", "")
            tables: dict[str, list[str]] = {}
            for k, v in data.items():
                if k == "template":
                    continue
                if isinstance(v, list) and v and isinstance(v[0], str):
                    tables[k] = [str(x) for x in v]
            return cls(template=template, tables=tables)
        return super().model_validate(obj, **kwargs)

    def roll(self, rng: _random.Random) -> str:
        """Compose a backstory by rolling each `{key}` slot in the template.

        For every key found in ``self.tables``, pick one entry uniformly
        with ``rng`` and substitute it for the ``{key}`` placeholder.
        Any leftover ``{key}`` placeholders (keys the pack didn't supply)
        are stripped along with the ``. `` or trailing whitespace that
        followed them, matching the behavior of the inline composer in
        CharacterBuilder.build().
        """
        result = self.template
        for key, entries in self.tables.items():
            if entries:
                pick = entries[rng.randrange(len(entries))]
                result = result.replace(f"{{{key}}}", pick)
        # Drop any unmatched {key} placeholders plus the punctuation/whitespace
        # immediately following so the prose stays clean.
        result = _re.sub(r"\{[^{}]+\}\s*\.?\s*", "", result)
        return result.strip()


class EquipmentTables(BaseModel):
    """Random equipment generation tables loaded from equipment_tables.yaml.

    `tables` is the top-level slot→items mapping consumed by
    `equipment_generation: random_table`. `class_tables` is a per-class
    override consumed by `equipment_generation: class_kit`; the chosen
    class's `kit_table` id resolves to one of these blocks.
    """

    model_config = {"extra": "forbid"}

    tables: dict[str, list[str]] = Field(default_factory=dict)
    rolls_per_slot: dict[str, int] = Field(default_factory=dict)
    class_tables: dict[str, dict[str, list[str]]] = Field(default_factory=dict)


class VisualStyle(BaseModel):
    """Image generation style configuration.

    Intentionally no extra="forbid" — genre packs may add flavor fields.
    """

    # Note: No extra="forbid" per Rust comment (visual_style_accepts_extra_fields).
    # Legacy LoRA YAMLs (still containing `lora:` / `lora_trigger:` / `loras:`)
    # remain loadable as opaque extras until Story 43-4 scrubs them.
    model_config = {"extra": "allow"}

    positive_suffix: str
    negative_prompt: str
    preferred_model: str
    base_seed: int
    visual_tag_overrides: dict[str, str] = Field(default_factory=dict)
