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
    # When True, this archetype is a SPECIFIC named individual (a real or
    # historical person — e.g. "Charles Taze Russell" — or a unique authored
    # figure), present for flavor/authoring reference. It must never be drawn
    # into a RANDOM spawn (encountergen enemies, namegen walk-ons): doing so
    # produced the "charles taze russell, Jewish" combat NPC (2026-06-01
    # playtest). Explicit ``--archetype`` requests may still target it. Opt-in;
    # default archetypes remain freely spawnable.
    named_individual: bool = False


def spawnable_archetypes(archetypes: list[NpcArchetype]) -> list[NpcArchetype]:
    """Archetypes eligible for RANDOM NPC/encounter generation.

    Excludes :attr:`NpcArchetype.named_individual` templates — specific real
    people / unique figures that must only be summoned by explicit request,
    never by ``rng.choice``. Callers that random-pick should fail loud when
    this returns empty rather than silently falling back to the full list.
    """
    return [a for a in archetypes if not a.named_individual]


class IdentityCapture(BaseModel):
    """Story-scene identity capture flags (pronouns + freeform fields).

    Used by the_story scene in genre packs that fold pronouns into a
    combined identity scene.
    """

    model_config = {"extra": "forbid"}

    pronouns_required: bool = True
    background_optional: bool = True
    description_optional: bool = True


class OriginTraitDef(BaseModel):
    """A world-authored origin trait granted by a chargen choice (story 89-5).

    The dual-voice shape mirrors AbilityDefinition: the builder seeds it onto
    Character.abilities with source=Race and emits the
    ``chargen.origin_trait.applied`` OTEL event. This is the documented
    world-tier crunch exception (Barsoom design D5/§9): the trait definition
    lives in a WORLD's char_creation.yaml choice — never keyed off a race
    string in engine code — and its mechanical halves ride pre-wired
    consumers (``stat_bonuses`` → generate_stats; the ability → the
    narrator/ability surface).
    """

    model_config = {"extra": "forbid"}

    name: str
    genre_description: str
    mechanical_effect: str
    involuntary: bool = False


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

    # World-tier origin trait (89-5): a chargen choice may grant a dual-voice
    # Race-source ability (e.g. the Barsoom Earthman gravity boon). Authored
    # in world char_creation.yaml; seeded by the builder with OTEL.
    origin_trait: OriginTraitDef | None = None

    # Stock chargen step (103-2, build plan §D-B): a choice on the stock
    # scene records the picked stock; a choice on a Saint-branch scene
    # records the Saint (103-1's deferred selection surface). The builder
    # accumulates both for the chargen-confirm mutation init.
    stock_id: str | None = None
    saint_id: str | None = None

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


class WwnEffortSource(BaseModel):
    """One WWN class-source contributing an Effort pool (SRD §1.4.4).

    A magic-using class draws Effort from one or more named sources (High
    Mage, Vowed, Elementalist, ...). Effort from one source cannot fuel
    another, so each source seeds its own pool. The pool max at chargen is
    ``effort_base + starting_skill_level + governing_attr_mod``.

    Copy-not-share with the B/X ``ClassMagicConfig`` — the WWN economy
    (Effort + casts/day) is a separate model, not the slot-table shape.
    """

    model_config = {"extra": "forbid"}

    source: str  # "high_mage" | "vowed" | "elementalist" ...
    governing_attr: str  # canonical WWN attr key, e.g. "WISDOM"
    relevant_skill: str  # e.g. "Magic"
    starting_skill_level: int  # chargen skill level for the Effort-max formula


class WwnClassMagic(BaseModel):
    """Per-class WWN magic data (Effort sources + spell economy tables).

    Lives on the class def, consumed by ``seed_wwn_magic`` at chargen to
    seed ``EffortPool``s and a ``SpellcastingState``. Copy-not-share with
    the B/X ``ClassMagicConfig``: WWN does NOT use ``slots_by_class_level``.
    The by-level dicts are str-keyed ("1".."10") because YAML/JSON flatten
    int keys to strings. ``prepared_by_level`` is capacity metadata consumed
    by the rest/prepare action (Plan 3).

    ``starting_prepared`` is the chargen seed: a list of spell ids the class
    prepares at character creation, before the first rest.  ``seed_wwn_magic``
    seeds ``SpellcastingState.prepared`` from this list, capped at the level-1
    prepared capacity (``prepared_by_level["1"]``).  When the key is absent no
    truncation is applied.  Defaults to ``[]`` so non-spellcasting subclasses
    and older packs that omit the field are unaffected.
    """

    model_config = {"extra": "forbid"}

    effort_sources: list[WwnEffortSource] = Field(default_factory=list)  # one per class-source
    casts_per_day_by_level: dict[str, int] = Field(default_factory=dict)  # "1": 1 ... "10": 6
    max_spell_level_by_level: dict[str, int] = Field(default_factory=dict)
    prepared_by_level: dict[str, int] = Field(default_factory=dict)
    starting_prepared: list[str] = Field(default_factory=list)  # spell ids seeded at chargen
    partial: bool = False  # Partial class: Effort -1, min 1


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
    wwn_magic: WwnClassMagic | None = None
    saving_throws: SavingThrowsTable | None = None
    # WWN Warrior-archetype marker (SRD §1.5.18). Task 11 sets warrior: true on
    # the Guardian class YAML; the Killing Blow + Veteran's Luck dispatch seams
    # gate on this flag. False by default so all existing non-wwn classes are
    # unaffected without any YAML edits.
    warrior: bool = False


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
    # Stock branching (103-2): the scene is presented only when a prior
    # choice carried a matching mechanical_effects.stock_id. The tag is a
    # FILTER (no stock chosen -> tagged scenes skipped), never a demand
    # that some stock exists — branching is authored data, not engine code.
    requires_stock: str | None = None
    # Stat-generation branching (103-3): same FILTER doctrine as
    # requires_stock — the scene is presented only when a prior choice
    # adopted a matching mechanical_effects.stat_generation (e.g. the
    # "roll_the_bones" rolling surface). Default-mode walks skip it.
    requires_stat_generation: str | None = None


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
    preferred_model: str
    base_seed: int
    visual_tag_overrides: dict[str, str] = Field(default_factory=dict)
