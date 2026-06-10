"""CharacterBuilder — state machine for genre-driven character creation.

ADR-015: builder FSM — the builder doesn't exist before ``new()`` and
is conceptually consumed by ``build()``. No IDLE or COMPLETE states;
construction and consumption are the boundaries.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from enum import StrEnum

from opentelemetry import trace

from sidequest.game.ability import AbilitySource
from sidequest.game.character import AbilityDefinition, Character, CreationAnswer
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
    hp_pool_from_config,
)
from sidequest.game.creature_core import (
    HpConfigMissingClassError as _CoreHpConfigMissingClassError,
)
from sidequest.game.ruleset.swn import swn_attribute_modifier
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.wwn_magic import EffortPool, SpellcastingState
from sidequest.genre.models.character import (
    BackstoryTables,
    CharCreationScene,
    ClassDef,
    EquipmentTables,
    MechanicalEffects,
    OriginTraitDef,
)
from sidequest.genre.models.rules import CwnConfig, EdgeConfig, RulesConfig
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
)
from sidequest.protocol.models import ClassRequirement, CreationChoice, RolledStat
from sidequest.protocol.types import NonBlankString
from sidequest.server.reference_anchors import reference_url_for_ability
from sidequest.telemetry.spans.reference import (
    reference_url_attached_span,
    reference_url_skipped_span,
)

# ---------------------------------------------------------------------------
# Class qualification
# ---------------------------------------------------------------------------


def qualifying_classes(
    stats: dict[str, int],
    classes: list[ClassDef],
) -> list[ClassDef]:
    """Return classes whose prime_requisite stat meets minimum_score.

    Pure function — no side effects, no genre-pack lookups. Pass the
    rolled stats dict and the pack's class list; receive the subset
    the player qualifies for. Empty list = nothing qualifies (caller
    decides whether to reroll).
    """
    return [c for c in classes if stats.get(c.prime_requisite, 0) >= c.minimum_score]


def qualifying_classes_arrangement(
    arrangement: dict[str, int | None],
    classes: list[ClassDef],
) -> list[ClassDef]:
    """Return classes whose prime_requisite is met by an in-progress arrangement.

    Same predicate as :func:`qualifying_classes` but tolerates ``None`` slot
    values (an arrangement still being filled). Unfilled slots are treated
    as 0 — they cannot satisfy any minimum.
    """
    return [c for c in classes if (arrangement.get(c.prime_requisite) or 0) >= c.minimum_score]


def seed_system_strain(rules: RulesConfig, stats: dict[str, int]) -> SystemStrainPool | None:
    """Return a SystemStrainPool for a CWN-family pack (max = CONSTITUTION-flavor score), else None.

    System Strain is a CwnConfig mechanic, shared by CWN and AWN (which subclasses
    CWN). We gate on the CAPABILITY (``isinstance(cfg, CwnConfig)``) rather than a
    slug string so AWN — and any future CWN sister module — gets a strain pool for
    free, instead of silently falling through the ``ruleset == "cwn"`` check.
    ``attribute_map["CONSTITUTION"]`` gives the flavor stat name (e.g. "Body");
    the pool max is clamped to at least 1. The ruleset's validator guarantees the
    CONSTITUTION key exists in attribute_map.
    """
    cfg = rules.ruleset_config()
    if not isinstance(cfg, CwnConfig):  # covers CWN + AWN + future subclasses
        return None
    con_flavor = cfg.attribute_map["CONSTITUTION"]  # validated present by the ruleset validator
    body_score = int(stats.get(con_flavor, 10))
    return SystemStrainPool(current=0, max=max(1, body_score), permanent=0)


def seed_wwn_magic(
    rules: RulesConfig,
    stats: dict[str, int],
    class_def: ClassDef | None,
) -> tuple[dict[str, EffortPool], SpellcastingState | None]:
    """Seed WWN Effort pools + spellcasting state at chargen (SRD §1.4.4, §4.2).

    Returns ``({}, None)`` for non-wwn rulesets and for non-magic classes
    (no ``wwn_magic`` on the class def) — no silent partial state.

    For a magic class:
      * One ``EffortPool`` per ``WwnEffortSource``, keyed by ``source.source``,
        with ``max = rules.wwn.magic.effort_base + source.starting_skill_level
        + swn_attribute_modifier(score-of-governing-attr)``. WWN shares the SWN
        attribute curve. A Partial class subtracts 1 (floor 1). The governing
        attr is a CANONICAL key ("WISDOM") resolved through
        ``rules.wwn.attribute_map`` to the pack's flavor stat name, then to the
        score in ``stats`` (mirrors ``seed_system_strain``).
      * A ``SpellcastingState`` from the level-1 cast tables (chargen level is
        always 1): ``casts_per_day`` / ``max_spell_level`` from the by-level
        dicts (default 0 if absent), ``casts_remaining = casts_per_day`` (full
        at chargen), ``prepared`` seeded from ``class_def.wwn_magic.starting_prepared``
        capped at the level-1 prepared capacity (``prepared_by_level["1"]``); when
        that key is absent no truncation is applied.  A class with effort sources
        but NO cast tables (an Effort-only Art user, e.g. the Vowed) yields
        ``spellcasting = None`` but still returns its effort dict.
        ``prepared_by_level`` is capacity metadata for the rest/prepare action.
    """
    if rules.ruleset != "wwn" or rules.wwn is None:
        return {}, None
    if class_def is None or class_def.wwn_magic is None:
        return {}, None

    cm = class_def.wwn_magic
    effort_base = rules.wwn.magic.effort_base
    attr_map = rules.wwn.attribute_map  # validated complete by _validate_wwn

    effort: dict[str, EffortPool] = {}
    for src in cm.effort_sources:
        flavor = attr_map[src.governing_attr]  # canonical -> flavor stat name
        score = int(stats.get(flavor, 10))
        pool_max = effort_base + src.starting_skill_level + swn_attribute_modifier(score)
        if cm.partial:
            pool_max -= 1  # Partial class: Effort -1
        pool_max = max(1, pool_max)  # WWN SRD: a caster's Effort is always at least 1
        effort[src.source] = EffortPool(source=src.source, max=pool_max)

    # Chargen level is always 1 (build() constructs level=1).
    level_key = "1"
    spellcasting: SpellcastingState | None = None
    # casts_per_day_by_level is the canonical "is this a spell-caster class" signal:
    # an Effort-only Art user (e.g. Vowed) has effort sources but no cast tables, so
    # max_spell_level_by_level may be absent — gate the whole state on casts only.
    if cm.casts_per_day_by_level:
        casts_per_day = cm.casts_per_day_by_level.get(level_key, 0)
        max_spell_level = cm.max_spell_level_by_level.get(level_key, 0)
        # Seed prepared from the class's starting_prepared list, capped at
        # the level-1 prepared capacity.  When "1" is absent in prepared_by_level,
        # len(cm.starting_prepared) is used as the fallback cap — effectively
        # no truncation.
        capacity = cm.prepared_by_level.get(level_key, len(cm.starting_prepared))
        prepared = cm.starting_prepared[:capacity]
        spellcasting = SpellcastingState(
            prepared=prepared,
            casts_remaining=casts_per_day,
            casts_per_day=casts_per_day,
            max_spell_level=max_spell_level,
        )

    return effort, spellcasting


def _seed_class_abilities(
    abilities: list[AbilityDefinition],
    class_def: ClassDef,
    *,
    pack_id: str | None = None,
) -> None:
    """Append Class-source signature abilities from class_def.abilities.

    Loader stamps source=AbilitySource.Class on each entry; authors do not
    type the discriminator. Empty class_def.abilities (e.g., Mage —
    signature lives in magic plugin) is a no-op.

    When ``pack_id`` is supplied the resulting AbilityDefinition carries a
    populated ``reference_url`` pointing to the class signature anchor on
    the rules reference page; an OTEL span is emitted for every ability.
    When ``pack_id`` is None the field is left as None — no span is
    emitted because there is no content-drift to signal.

    Spec: docs/superpowers/specs/2026-05-10-class-mechanical-surface-design.md §6.1.
    """
    for ca in class_def.abilities:
        url = (
            reference_url_for_ability(
                pack=pack_id or "",
                source="Class",
                ability_name=ca.name,
                owning_class_name=class_def.display_name,
            )
            if pack_id
            else None
        )

        if url is not None:
            with reference_url_attached_span(
                kind="ability",
                pack=pack_id or "",
                world=None,
                keys=(class_def.display_name, ca.name),
            ):
                pass
        elif pack_id is not None:
            # pack_id was supplied but URL could not be built — content drift.
            with reference_url_skipped_span(
                kind="ability",
                pack=pack_id,
                world=None,
                keys=(class_def.display_name, ca.name),
                reason="url_build_failed",
            ):
                pass

        abilities.append(
            AbilityDefinition(
                name=ca.name,
                genre_description=ca.genre_description,
                mechanical_effect=ca.mechanical_effect,
                involuntary=ca.involuntary,
                source=AbilitySource.Class,
                reference_url=url,
            )
        )


def _seed_item_abilities(abilities: list[AbilityDefinition], kit_def: object) -> None:
    """Populate Item-source abilities from the starting kit.

    DOCUMENTED STUB — empty body retained as the architectural seam for
    the imminent next story (item-source ability content). Removing this
    function or its call site requires updating spec
    docs/superpowers/specs/2026-05-10-class-mechanical-surface-design.md §6.2.
    Reviewer guard: contract test in tests/game/test_chargen_class_abilities.py.
    """
    return None


# ---------------------------------------------------------------------------
# Narrative hook extraction
# ---------------------------------------------------------------------------


class HookType(StrEnum):
    """Category of narrative hook."""

    ORIGIN = "Origin"
    """From race_hint."""
    WOUND = "Wound"
    """From backstory trauma."""
    RELATIONSHIP = "Relationship"
    """From relationship effects."""
    GOAL = "Goal"
    """From goals effects."""
    TRAIT = "Trait"
    """From class_hint or personality_trait."""
    DEBT = "Debt"
    """From obligation effects."""
    SECRET = "Secret"
    """From hidden knowledge."""
    POSSESSION = "Possession"
    """From equipment_hints / item_hint."""


@dataclass
class NarrativeHook:
    """A narrative hook derived from character creation choices."""

    hook_type: HookType
    source_scene: str
    text: str
    mechanical_key: str | None = None


@dataclass
class LoreAnchor:
    """A connection to the game world (faction, NPC, location).

    anchor_type: "faction", "npc_relationship", "location" or similar.
    """

    anchor_type: str
    value: str
    source_scene: str


# ---------------------------------------------------------------------------
# Scene input — tagged union for "how the player responded"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SceneInputType:
    """Sealed base for scene input variants. Use the concrete subclasses."""


@dataclass(frozen=True)
class ChoiceInput(SceneInputType):
    """Player selected a numbered choice."""

    index: int


@dataclass(frozen=True)
class FreeformInput(SceneInputType):
    """Player typed freeform text."""

    text: str


@dataclass(frozen=True)
class StoryInput(SceneInputType):
    """Player submitted the_story scene (pronouns + freeform background/description).

    Used by genre packs that fold pronouns into a combined identity scene
    (the_story). The scene's
    ``mechanical_effects.identity_capture.pronouns_required`` gates whether
    ``pronouns`` may be empty.
    """

    pronouns: str
    background: str
    description: str


# Indefinite/definite articles that mark a chargen CHOICE label as an oblique
# flavor phrase rather than a vocation title. heavy_metal's calling scene uses
# evocative phrases ("A craft that costs the craftsman") whose real class lives
# in ``class_hint``; spaghetti_western uses thematic paths ("The Gun" → Gunslinger);
# wry_whimsy echoes the class with an article ("A curious child" → "Curious Child").
# A genuine vocation display label ("Country Veterinary Surgeon", "Channeler")
# never leads with an article. See ``_is_vocation_label``.
_ARTICLE_PREFIXES = ("a ", "an ", "the ")


def _is_vocation_label(label: str) -> bool:
    """True when a choice label reads like a vocation title fit for the Calling
    display, False when it's an oblique flavor phrase that should fall back to
    the resolved ``class_hint``.

    The discriminator is a leading indefinite/definite article. A label like
    "A craft that costs the craftsman" is a feeling the player chose, not a job
    name — stamping it as the Calling produced the doubled-article bug
    ("Vesska, a A craft that costs the craftsman"). Falling back to the
    ``class_hint`` ("Elementalist") that every such choice already carries is
    strictly cleaner across every pack that does this (heavy_metal,
    spaghetti_western, wry_whimsy). Combined-origin packs (elemental_harmony
    "The Ember Isles" → race+class) are already excluded upstream by the
    ``race_hint is None`` guard at the capture site.
    """
    return not label.strip().lower().startswith(_ARTICLE_PREFIXES)


# The race-axis discriminator is INDEFINITE-only — see _is_origin_display_label.
_INDEFINITE_ARTICLE_PREFIXES = ("a ", "an ")


def _is_origin_display_label(label: str) -> bool:
    """True when a choice label works as the origin/Race display, False when
    it's an indefinite-article descriptor phrase that should fall back to the
    resolved ``race_hint``.

    Race-axis sibling of ``_is_vocation_label`` (sq-playtest 2026-06-10,
    barsoom): "A Green Martian of the Hordes" stamped verbatim made the sheet
    read "Race: A Green Martian of the Hordes" where the resolved race_hint
    ("Green Martian") belongs. Unlike the Calling guard this one keys on the
    INDEFINITE article only — a full-corpus survey shows every "a/an" origin
    label reads better as its race_hint ("A Sealed Vault" → Pure Strain Human,
    "A Lab" → Synthetic, all five barsoom origins), while DEFINITE-article
    labels are intended displays across the packs ("The Village Itself" over
    Servant, "The Streets" over Street, elemental_harmony's "The …" homelands
    — the documented reason race_label exists). Do not widen to "the".
    """
    return not label.strip().lower().startswith(_INDEFINITE_ARTICLE_PREFIXES)


# ---------------------------------------------------------------------------
# SceneResult — unit of revert for go_back
# ---------------------------------------------------------------------------


@dataclass
class SceneResult:
    """What a single scene produced — the unit of revert.

    ``choice_description`` stores the flavor description text from the
    chosen option so we can compose a narrative backstory instead of
    only keeping the mechanical label.

    ``choice_label`` stores the short option label ("Someone Went Into the
    Drift", "Vault Dweller", etc.) — needed by the chargen-preview to
    display the chosen backstory hook on genres whose backstory scene
    doesn't write to ``MechanicalEffects.background`` (e.g. space_opera,
    tea_and_murder). ``None`` for freeform inputs that have no label.
    """

    input_type: SceneInputType
    effects_applied: MechanicalEffects
    hooks_added: list[NarrativeHook] = field(default_factory=list)
    anchors_added: list[LoreAnchor] = field(default_factory=list)
    choice_description: str | None = None
    choice_label: str | None = None
    # Display-only vocation label derived from freeform text on a
    # class-selecting scene (every canned choice carries class_hint, but the
    # freeform path carries none). Feeds the {class} prose slot WITHOUT
    # setting the mechanical class_hint — so the starting-loadout class match
    # keeps resolving to the pack default. Symmetric with background_label.
    freeform_class_label: str | None = None
    # Optional source-scene id. Populated by paths that need to identify
    # which scene produced this result without indexing back through the
    # scene list (e.g. the_story's StoryInput dispatch). Older paths leave
    # this as None — scene order is implicit in the results list.
    scene_id: str | None = None
    # Name-scene followup correction (playtest 2026-06-05 RW-2). When the
    # name-entry scene has a hook_prompt, the followup answer is the player's
    # name correction — stored here so character_name()/vessel_name() can
    # merge it over the original parse. Non-name scenes keep the legacy
    # followup-as-wound-hook behavior and leave this None.
    followup_text: str | None = None


# ---------------------------------------------------------------------------
# AccumulatedChoices — compacted view across all completed scenes
# ---------------------------------------------------------------------------


@dataclass
class AccumulatedChoices:
    """Accumulated mechanical effects across all completed scenes.

    Most hint fields follow last-one-wins semantics (a later scene
    overrides an earlier one). Lists and stat_bonuses accumulate.

    ``reputation_bonus`` wires the Phase 1 IOU (character.py:66) —
    spaghetti_western chargen choices tag ``reputation_bonus``; the
    builder accumulates it alongside other hints. The downstream
    reputation system is still post-Phase-2; the value simply flows
    through for now.
    """

    class_hint: str | None = None
    # Display-only vocation label from a freeform answer on a class-selecting
    # scene. Used by {class} prose substitution when the mechanical class_hint
    # is absent (free-text path). The mechanical char_class still resolves via
    # class_hint-or-default. Symmetric with background_label.
    class_label: str | None = None
    race_hint: str | None = None
    # Display-only origin label — the choice LABEL of the scene whose
    # MechanicalEffects.race_hint produced the mechanical origin archetype.
    # Symmetric with background_label / class_label. tea_and_murder's
    # "origins" scene maps rich flavor buttons ("The Village Itself") onto a
    # small mechanical taxonomy (race_hint: Servant); the player-facing
    # chargen summary should surface the chosen flavor, not the raw archetype
    # slug (sq-playtest 2026-05-28 BUG-LOW). The mechanical Character.race
    # still resolves from race_hint — this is display-only. Last-wins.
    race_label: str | None = None
    personality_trait: str | None = None
    item_hints: list[str] = field(default_factory=list)
    affinity_hint: str | None = None
    background: str | None = None
    # Captured alongside ``background`` — the choice LABEL of the scene
    # whose ``MechanicalEffects.background`` produced the mechanical tag.
    # Symmetric with ``backstory_label`` below. Used by Character.background
    # (canned-openings P2) so Opening triggers.backgrounds (which match the
    # validator-derived ``chargen_backgrounds`` LABEL list) can filter
    # correctly. Last-wins like other single-value hints.
    background_label: str | None = None
    # Detected from scene effects shape — set when a scene's
    # MechanicalEffects looks "backstory-hook-shaped" (touches
    # relationship/goals/emotional_state, doesn't touch race/class/
    # mutation/rig hints). Records the choice LABEL of that scene so
    # the chargen preview can show the chosen backstory hook
    # ("Someone Went Into the Drift") instead of the origin routing
    # tag ("Outsystem-arrived"). Last-wins like other single-value
    # hints. Genres without a drive-shaped scene leave this None and
    # the preview falls back to ``background`` — matches the
    # mutant_wasteland pattern where ``background`` IS the meaningful
    # label ("Vault Dweller", "Heap Rat").
    backstory_label: str | None = None
    mutation_hint: str | None = None
    # World-tier origin trait (89-5): dual-voice Race-source ability carried
    # by a chargen choice (e.g. the Barsoom Earthman gravity boon).
    # Last-wins like other single-value hints.
    origin_trait: OriginTraitDef | None = None
    training_hint: str | None = None
    emotional_state: str | None = None
    relationship: str | None = None
    goals: str | None = None
    rig_type_hint: str | None = None
    rig_trait: str | None = None
    catch_phrase: str | None = None
    backstory_fragments: list[str] = field(default_factory=list)
    stat_bonuses: dict[str, int] = field(default_factory=dict)
    pronoun_hint: str | None = None
    jungian_hint: str | None = None
    rpg_role_hint: str | None = None
    reputation_bonus: str | None = None


# ---------------------------------------------------------------------------
# BuilderPhase — tagged state machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuilderPhase:
    """Sealed base for builder phase variants. Use the concrete subclasses."""


@dataclass(frozen=True)
class InProgress(BuilderPhase):
    """Processing genre-defined scenes."""

    scene_index: int


@dataclass(frozen=True)
class AwaitingFollowup(BuilderPhase):
    """Scene has a hook_prompt — waiting for player's followup text."""

    scene_index: int
    hook_prompt: str


@dataclass(frozen=True)
class Confirmation(BuilderPhase):
    """All scenes done, showing summary for confirmation."""


# Singleton instance of Confirmation — it carries no data, so sharing is fine.
CONFIRMATION: Confirmation = Confirmation()


# ---------------------------------------------------------------------------
# BuilderError — typed exception hierarchy
# ---------------------------------------------------------------------------


class BuilderError(Exception):
    """Base class for CharacterBuilder errors.

    Each variant maps to a subclass so callers can catch specific
    failure modes::

        try:
            builder.apply_choice(idx)
        except BuilderError.InvalidChoice as e:
            ...

    The nested subclass attributes (BuilderError.InvalidChoice, etc.) are
    aliases for the module-level classes; they exist so call sites don't
    need to import every variant separately.
    """


class InvalidChoiceError(BuilderError):
    """Choice index out of range."""

    def __init__(self, index: int, max_index: int) -> None:
        self.index = index
        self.max_index = max_index
        super().__init__(f"invalid choice: index {index} but max is {max_index}")


class WrongPhaseError(BuilderError):
    """Operation not valid in the current phase."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"wrong phase: expected {expected}, got {actual}")


class FreeformNotAllowedError(BuilderError):
    """Freeform input not allowed for this scene."""

    def __init__(self) -> None:
        super().__init__("freeform input not allowed for this scene")


class NoScenesError(BuilderError):
    """No scenes provided to the builder."""

    def __init__(self) -> None:
        super().__init__("no scenes provided")


class CannotRevertError(BuilderError):
    """Cannot revert — already at the first scene."""

    def __init__(self) -> None:
        super().__init__("cannot revert: already at first scene")


class UnknownStatGenerationError(BuilderError):
    """Unrecognized stat generation method."""

    def __init__(self, method: str) -> None:
        self.method = method
        super().__init__(f"unknown stat generation method: {method}")


class NumericNameError(BuilderError):
    """Name is purely numeric — likely a UI index, not a real character name.

    Story 30-1: Reject purely numeric names — they indicate a UI choice
    index was used as the name fallback instead of a real character name.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"invalid character name: '{name}' is purely numeric (likely a UI index, not a name)"
        )


class HpConfigMissingClassError(BuilderError):
    """Genre pack declared an HP config but omitted a `base_max_by_class`
    entry for the character's class. Fails chargen loudly (ADR-114) —
    silently reverting to a default would hide content bugs.
    """

    def __init__(self, class_name: str) -> None:
        self.class_name = class_name
        super().__init__(f"hp base_max_by_class missing entry for class '{class_name}'")


class PoolValueNotPresentError(Exception):
    """assign_stat called with a value not currently in the arrangement pool."""


class UnfilledArrangementError(Exception):
    """confirm_arrangement called before all six slots are filled."""


class NoQualifyingClassesError(Exception):
    """confirm_arrangement called but the arrangement qualifies for no classes."""


class ArrangementSceneActiveError(Exception):
    """apply_response called while the_arrangement scene is active.

    Arrangement scenes (mechanical_effects.assignment_required=True) only
    accept apply_arrangement_confirm / apply_arrangement_reject — generic
    ChoiceInput / FreeformInput are not valid here.
    """


# Attach subclass aliases so callers can write `BuilderError.InvalidChoice`
# in catch blocks without importing each variant.
BuilderError.InvalidChoice = InvalidChoiceError  # type: ignore[attr-defined]
BuilderError.WrongPhase = WrongPhaseError  # type: ignore[attr-defined]
BuilderError.FreeformNotAllowed = FreeformNotAllowedError  # type: ignore[attr-defined]
BuilderError.NoScenes = NoScenesError  # type: ignore[attr-defined]
BuilderError.CannotRevert = CannotRevertError  # type: ignore[attr-defined]
BuilderError.UnknownStatGeneration = UnknownStatGenerationError  # type: ignore[attr-defined]
BuilderError.NumericName = NumericNameError  # type: ignore[attr-defined]
BuilderError.HpConfigMissingClass = HpConfigMissingClassError  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Hook / anchor extraction (pure helpers)
# ---------------------------------------------------------------------------


def extract_hooks(scene_id: str, effects: MechanicalEffects) -> list[NarrativeHook]:
    """Derive narrative hooks from mechanical effects on a chosen option.

    Each produced hook records the ``mechanical_key`` that generated it
    so the ``build()`` finalizer can filter hooks already represented on
    the character sheet (race, class, personality).
    """
    hooks: list[NarrativeHook] = []

    if effects.race_hint is not None:
        hooks.append(
            NarrativeHook(
                hook_type=HookType.ORIGIN,
                source_scene=scene_id,
                text=f"Origin: {effects.race_hint}",
                mechanical_key="race_hint",
            )
        )

    if effects.class_hint is not None:
        hooks.append(
            NarrativeHook(
                hook_type=HookType.TRAIT,
                source_scene=scene_id,
                text=f"Class: {effects.class_hint}",
                mechanical_key="class_hint",
            )
        )

    if effects.personality_trait is not None:
        hooks.append(
            NarrativeHook(
                hook_type=HookType.TRAIT,
                source_scene=scene_id,
                text=f"Personality: {effects.personality_trait}",
                mechanical_key="personality_trait",
            )
        )

    if effects.relationship is not None:
        hooks.append(
            NarrativeHook(
                hook_type=HookType.RELATIONSHIP,
                source_scene=scene_id,
                text=f"Relationship: {effects.relationship}",
                mechanical_key="relationship",
            )
        )

    if effects.goals is not None:
        hooks.append(
            NarrativeHook(
                hook_type=HookType.GOAL,
                source_scene=scene_id,
                text=f"Goal: {effects.goals}",
                mechanical_key="goals",
            )
        )

    if effects.item_hint is not None:
        hooks.append(
            NarrativeHook(
                hook_type=HookType.POSSESSION,
                source_scene=scene_id,
                text=f"Item: {effects.item_hint}",
                mechanical_key="item_hint",
            )
        )

    return hooks


def extract_anchors(scene_id: str, effects: MechanicalEffects) -> list[LoreAnchor]:
    """Derive lore anchors (world-graph links) from mechanical effects.

    Relationship effects imply NPC anchors — if the choice names a
    mentor or rival, that name becomes a future lore seed.
    """
    anchors: list[LoreAnchor] = []
    if effects.relationship is not None:
        anchors.append(
            LoreAnchor(
                anchor_type="npc",
                value=effects.relationship,
                source_scene=scene_id,
            )
        )
    return anchors


# ---------------------------------------------------------------------------
# String helpers (module-level, pure)
# ---------------------------------------------------------------------------


def humanize_snake_case(s: str) -> str:
    """Convert a snake_case identifier to Title Case display name.

    E.g. "natural_armor" → "Natural Armor",
         "mystery_compass" → "Mystery Compass".
    """
    return " ".join(word.capitalize() if word else "" for word in s.split("_"))


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'First Middle Last' → ('First', 'Middle Last'). Empty → ('', '').

    Used by ``CharacterBuilder.build`` to populate
    ``Character.first_name`` / ``Character.last_name`` for the
    canned-openings chassis-voice block. No nickname source today —
    that field stays empty by design.
    """
    parts = full_name.strip().split()
    if not parts:
        return ("", "")
    return (parts[0], " ".join(parts[1:]))


def strip_unmatched_placeholders(s: str) -> str:
    """Strip any unmatched `{key}` placeholders and orphan trailing
    punctuation/whitespace from a substituted template.

    After a template has had every known table key substituted, any
    remaining `{key}` placeholders correspond to keys the genre pack didn't
    supply. The literal "{feature}" would otherwise leak into user-facing
    prose. Drop the placeholder and consume any immediately-following `. `,
    `, `, or bare whitespace so we don't leave "Former ratcatcher. . ." in
    the output.

    Unbalanced placeholders (no closing `}`) preserve the literal `{` so
    the bug is visible rather than silently swallowed — SOUL.md: "Fail
    loud at the boundary."

    Reviewer finding from story 31-2.
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != "{":
            out.append(c)
            i += 1
            continue
        # Skip to the matching '}' (or end of string if unbalanced).
        close = s.find("}", i + 1)
        if close == -1:
            # Unbalanced — keep the literal '{' and stop scanning.
            out.append("{")
            break
        # Advance past the '}' and eat orphan trailing punctuation/whitespace.
        i = close + 1
        while i < n and s[i] in (".", ",", " "):
            i += 1

    # Collapse internal whitespace runs and trim leading/trailing whitespace.
    return " ".join("".join(out).split())


def find_unrecognized_tokens(rendered: str) -> list[str]:
    """Scan interpolated narration for placeholders the interpolator didn't resolve.

    Used by CharacterBuilder.interpolate_scene_narration to surface author-typo'd
    or unsupported placeholder keys via one OTEL Warn event per offending token.
    Returning only the first match would let a second typo in the same narration
    leak silently to the client; this scanner is exhaustive by contract.

    Recognized tokens are {name}, {class}, {race} — anything else (e.g. a typo'd
    {nmae}, or an unsupported key like {origin}) is returned literally, including
    the surrounding braces. An unclosed `{` at the tail is returned as the rest
    of the string so the malformed token surfaces rather than silently truncates.
    """
    out: list[str] = []
    i = 0
    n = len(rendered)
    while i < n:
        if rendered[i] != "{":
            i += 1
            continue
        close = rendered.find("}", i + 1)
        if close == -1:
            # Unclosed — surface the remainder as a single bad token and stop.
            out.append(rendered[i:])
            break
        token_end = close + 1
        token = rendered[i:token_end]
        if token not in ("{name}", "{class}", "{race}"):
            out.append(token)
        i = token_end
    return out


_CLASS_LABEL_DELIMITERS = ("—", "–", " - ", "\n", ".", ";", ",")
_CLASS_LABEL_ARTICLES = ("a ", "an ", "the ")


def derive_class_label(text: str) -> str:
    """Derive a short vocation label from freeform chargen text.

    The {class} prose slot expects a noun phrase ("a {class}'s working life").
    A freeform vocation answer is often a full sentence with flavor — e.g.
    "A vegetarian and temperance lecturer — earnest, melancholy, forever
    ignored." This trims to the leading role phrase: cut at the first
    delimiter (em/en-dash, " - ", newline, period, semicolon, comma), strip a
    leading article (so the template's own "a {class}" doesn't double up), and
    collapse internal whitespace.

    Returns "" when the text yields no role phrase (caller decides whether to
    fall through to class_hint).
    """
    label = text.strip()
    cut = len(label)
    for delim in _CLASS_LABEL_DELIMITERS:
        idx = label.find(delim)
        if idx != -1:
            cut = min(cut, idx)
    label = " ".join(label[:cut].split())
    lowered = label.lower()
    for article in _CLASS_LABEL_ARTICLES:
        if lowered.startswith(article):
            label = label[len(article) :].lstrip()
            break
    return label


def indefinite_article(word: str) -> str:
    """Return "a" or "an" for the leading sound of ``word``.

    Letter-based heuristic on the first character (good enough for the
    race/class identity line — "Ember Isles" → "an", "Channeler" → "a"). Not a
    full phonetic library; it does not special-case "hour"/"university"-class
    exceptions, which do not occur in race/class slugs.
    """
    stripped = word.lstrip()
    if stripped and stripped[0].lower() in "aeiou":
        return "an"
    return "a"


# A proper-noun-ish token ("Zeppo", "V8", "D'Arcy") and a 1-4 token phrase
# ("Mad Max", "Duck Soup", "Snake Plissken"). Deliberately case-SENSITIVE —
# the keyword prefixes below match case-insensitively via scoped (?i:) groups,
# but the captured name itself must look like a proper noun, which is what
# terminates the capture at the first lowercase word ("Duck Soup — because…"
# stops after "Soup").
_NAME_TOKEN = r"[A-Z0-9][\w'’\-]*"
_NAME_PHRASE = rf"{_NAME_TOKEN}(?: {_NAME_TOKEN}){{0,3}}"

# Rider-name patterns, priority order. The generic "name:" form excludes a
# preceding "rig " / "rig's " so "Rig name: Duck Soup" never bleeds into the
# rider half.
_RIDER_NAME_PATTERNS = (
    re.compile(rf"(?i:\b(?:road|rider)\s+name\s*(?:is|[:=])\s*)({_NAME_PHRASE})"),
    re.compile(rf"(?i:(?<!rig )(?<!rig's )\bname\s*(?:is|[:=])\s*)({_NAME_PHRASE})"),
    re.compile(
        rf"(?i:\b(?:they call me|call me|i'?m called|i am called|i go by|go by|known as|name's)\s+)"
        rf"({_NAME_PHRASE})"
    ),
)

# Vessel/rig-name patterns, priority order.
_VESSEL_NAME_PATTERNS = (
    re.compile(rf"(?i:\brig(?:'s)?\s+name\s*(?:is|[:=])\s*)({_NAME_PHRASE})"),
    re.compile(rf"(?i:\b(?:the\s+)?rig\s*[:=]\s*)({_NAME_PHRASE})"),
    re.compile(rf"(?i:\b(?:the\s+)?rig\s+is(?:\s+called)?\s+)({_NAME_PHRASE})"),
)

# Whole-text fallbacks: a bare name ("Kara", "Mad Max") and the two-part
# comma answer to the two-part question ("Zeppo, Duck Soup").
_PLAIN_NAME_RE = re.compile(rf"^({_NAME_PHRASE})[.!]?$")
_COMMA_PAIR_RE = re.compile(rf"^({_NAME_PHRASE}),\s*({_NAME_PHRASE})[.!]?$")
# Leading short segment before sentence punctuation ("Zeppo. The rig: …").
# Only consulted when a vessel name was found — the two-part answer shape —
# so arbitrary prose never gets its first sentence promoted to a name.
_LEADING_NAME_RE = re.compile(rf"^({_NAME_PHRASE})\s*[.!;,]")


def extract_freeform_names(text: str) -> tuple[str | None, str | None]:
    """Parse a freeform name-scene answer into (character_name, vessel_name).

    The name-entry scene can ask a two-part question (road_warrior the_name:
    "What do they call you? And what do they call the rig?") and players
    answer in prose. Deterministic best-effort extraction of the observed
    phrasings (playtest 2026-06-05 RW-2):

      "They call me Zeppo. The rig is Duck Soup — because…"  → both halves
      "Road name: Zeppo. Rig name: Duck Soup."               → both halves
      "Zeppo. The rig: Duck Soup."                           → both halves
      "Zeppo, Duck Soup"                                     → both halves
      "Kara"                                                 → name only

    Returns (None, None) when nothing name-like is recognized — the caller
    decides the fallback (``character_name()`` keeps the legacy verbatim
    text) rather than this helper guessing silently.
    """
    trimmed = text.strip()
    if not trimmed:
        return (None, None)

    name: str | None = None
    vessel: str | None = None
    for pattern in _RIDER_NAME_PATTERNS:
        m = pattern.search(trimmed)
        if m:
            name = m.group(1)
            break
    for pattern in _VESSEL_NAME_PATTERNS:
        m = pattern.search(trimmed)
        if m:
            vessel = m.group(1)
            break

    # Two-part comma answer — fill only the missing halves.
    if name is None and vessel is None:
        m = _COMMA_PAIR_RE.match(trimmed)
        if m:
            return (m.group(1), m.group(2))

    # Bare name.
    if name is None:
        m = _PLAIN_NAME_RE.match(trimmed)
        if m:
            name = m.group(1)

    # Two-part answer where the rider half is an unprefixed leading segment
    # ("Zeppo. The rig: Duck Soup.") — gated on the vessel half having
    # matched so plain prose never promotes its first words to a name.
    if name is None and vessel is not None:
        m = _LEADING_NAME_RE.match(trimmed)
        if m:
            name = m.group(1)

    return (name, vessel)


# ---------------------------------------------------------------------------
# CharacterBuilder — the state machine
# ---------------------------------------------------------------------------


class CharacterBuilder:
    """State machine for character creation driven by genre-pack scenes.

    Tracks scene progression, accumulates mechanical effects, extracts
    narrative hooks, and ultimately produces a ``Character`` via
    ``build()``.
    """

    def __init__(
        self,
        scenes: list[CharCreationScene],
        rules: RulesConfig,
        backstory_tables: BackstoryTables | None = None,
        *,
        rng: random.Random | None = None,
    ) -> None:
        """Create a new builder.

        Raises ``NoScenesError`` if ``scenes`` is empty.

        ``rng`` is a seeded RNG source for deterministic stat generation
        in tests. Production callers should omit it (defaults to a fresh
        ``random.Random()``).
        """
        if not scenes:
            raise NoScenesError()

        self._scenes: list[CharCreationScene] = scenes
        self._results: list[SceneResult] = []
        self._phase: BuilderPhase = InProgress(scene_index=0)
        self._rng: random.Random = rng if rng is not None else random.Random()
        # Stash the full rules ref so summary rendering can pull
        # vocabulary fields (``chargen_field_labels``) without having
        # to thread the GenrePack rules separately. Existing per-attr
        # snapshots below preserve the original behavior of allowing
        # scene directives to override stat_generation at apply time
        # without mutating the pack-shared rules object.
        self._rules: RulesConfig = rules

        # Configuration sourced from RulesConfig. Keep these as attributes
        # (not a stored reference) so scene directives can override
        # stat_generation at apply time.
        self._stat_generation: str = rules.stat_generation
        self._ability_score_names: list[str] = list(rules.ability_score_names)
        self._default_class: str | None = rules.default_class
        self._default_race: str | None = rules.default_race
        self._edge_config: EdgeConfig | None = rules.edge_config
        self._point_buy_budget: int = rules.point_buy_budget
        self._race_label: str = rules.race_label or "Race"
        self._class_label: str = rules.class_label or "Class"

        # Eager roll at construction — scan scenes for the first
        # `stat_generation: roll_3d6_strict` directive so stat values
        # are available for narration injection when the declaring scene
        # is first rendered. The scene content is authoritative: if a
        # scene declares roll_3d6_strict, that scene's narration gets
        # stat values.
        self._rolled_stats: list[tuple[str, int]] | None = None
        # Arrange-visible mode: pool is a list of six 3d6 totals,
        # unassigned. Arrangement happens via assign_stat / clear_stat,
        # confirmed via confirm_arrangement, rejected via reject_arrangement.
        self._arrangement_pool: list[int] | None = None
        self._arrangement_assignment: dict[str, int | None] | None = None
        self._classes: list[ClassDef] = []
        for s in scenes:
            eff = s.mechanical_effects
            if eff is None or eff.stat_generation is None:
                continue
            if eff.stat_generation == "roll_3d6_strict":
                self._roll_3d6_strict()
            elif eff.stat_generation == "roll_3d6_arrange_visible":
                self._roll_3d6_arrange_visible()
            break

        self._backstory_tables: BackstoryTables | None = backstory_tables
        self._equipment_tables: EquipmentTables | None = None
        self._lobby_name: str | None = None
        self._pack_id: str | None = None

    # --- Fluent setters ---

    def with_lobby_name(self, name: str) -> CharacterBuilder:
        """Attach the lobby-provided player name.

        Used as a fallback for the `{name}` placeholder in scene narration
        when the genre has no name-entry scene (heavy_metal,
        caverns_and_claudes). Fluent setter — chain after construction.

        Blank / whitespace-only names clear the attribute so interpolation
        falls through to the scene-entered name.
        """
        trimmed = name.strip()
        self._lobby_name = trimmed if trimmed else None
        return self

    def with_equipment_tables(self, tables: EquipmentTables) -> CharacterBuilder:
        """Attach random equipment tables.

        When set AND a scene declares `equipment_generation: random_table`,
        the Slice 4 build() finalizer will roll starting inventory from
        these tables. Story 31-3.
        """
        self._equipment_tables = tables
        return self

    def with_classes(self, classes: list[ClassDef]) -> CharacterBuilder:
        """Attach the genre pack's class definitions for qualification loop
        and class_kit equipment selection."""
        self._classes = list(classes)
        return self

    def with_pack_id(self, pack_id: str) -> CharacterBuilder:
        """Record the genre pack slug so build() can attach reference_url on
        Class-source signature abilities (Task 6, reference-pages v2)."""
        self._pack_id = pack_id
        return self

    # --- Autogen helpers ---

    def autogen_backstory(self, seed: int) -> dict[str, str]:
        """Roll the pack's backstory tables for a deterministic background.

        No Claude call — pure table roll. Returns a dict with two keys:
        ``background`` (the composed text from the pack's template) and
        ``description`` (currently always empty string; reserved for
        future packs that supply a separate description-table format).

        Returns ``{"background": "", "description": ""}`` if no
        ``BackstoryTables`` are attached.
        """
        if self._backstory_tables is None:
            return {"background": "", "description": ""}
        local_rng = random.Random(seed)
        bg = self._backstory_tables.roll(local_rng)
        return {"background": bg, "description": ""}

    # --- Phase queries ---

    def is_in_progress(self) -> bool:
        """Whether the builder is in InProgress phase."""
        return isinstance(self._phase, InProgress)

    def is_awaiting_followup(self) -> bool:
        """Whether the builder is awaiting a followup answer."""
        return isinstance(self._phase, AwaitingFollowup)

    def is_confirmation(self) -> bool:
        """Whether the builder is in Confirmation phase."""
        return isinstance(self._phase, Confirmation)

    def current_scene_index(self) -> int:
        """Current scene index (0-based). Returns len(scenes) at Confirmation."""
        match self._phase:
            case InProgress(scene_index=i):
                return i
            case AwaitingFollowup(scene_index=i):
                return i
            case Confirmation():
                return len(self._scenes)
            case _:  # pragma: no cover — exhaustive
                raise AssertionError(f"unknown phase: {self._phase!r}")

    def current_scene(self) -> CharCreationScene:
        """Reference to the current scene definition.

        In Confirmation phase the builder is past the last scene; callers
        should branch on is_confirmation() before reading current_scene().

        When the scene's choices are class-hint encoded AND classes have
        been attached via with_classes(), the returned scene's choices
        are filtered to qualifying classes only. This keeps current_scene,
        apply_choice, and the wire protocol all reading from the same
        filtered view — preventing index drift between UI and server.
        """
        return self._filter_class_choices(self._scenes[self.current_scene_index()])

    def total_scenes(self) -> int:
        """Total number of scenes."""
        return len(self._scenes)

    def scenes(self) -> list[CharCreationScene]:
        """The raw scene definitions (used for lore seeding).

        Returns a shallow copy so callers cannot mutate builder state.
        """
        return list(self._scenes)

    def scene_results(self) -> list[SceneResult]:
        """The accumulated scene results stack.

        Returns a shallow copy so callers cannot mutate builder state.
        """
        return list(self._results)

    def current_hook_prompt(self) -> str | None:
        """Get the current hook prompt text, if awaiting followup."""
        if isinstance(self._phase, AwaitingFollowup):
            return self._phase.hook_prompt
        return None

    def rolled_stats(self) -> list[tuple[str, int]] | None:
        """Pre-rolled stats from roll_3d6_strict generation, if any.

        Exposed so external renderers (e.g. the confirmation summary
        composer) can read stats without reaching into private fields.

        Slice 3 wires the actual roll at construction — this reads None
        in Slice 2.
        """
        return list(self._rolled_stats) if self._rolled_stats is not None else None

    def arrangement_pool(self) -> list[int] | None:
        """Return the six unassigned 3d6 totals, or None if not in arrange mode."""
        return list(self._arrangement_pool) if self._arrangement_pool is not None else None

    def arrangement_assignment(self) -> dict[str, int | None] | None:
        """Return the current arrangement (stat → value-or-None)."""
        if self._arrangement_assignment is None:
            return None
        return dict(self._arrangement_assignment)

    def assign_stat(self, stat_name: str, value: int) -> None:
        """Move ``value`` from the arrangement pool into ``stat_name``.

        If the slot already has a value, that value is returned to the pool
        first. Raises ``PoolValueNotPresentError`` if ``value`` isn't in the
        pool.
        """
        if self._arrangement_pool is None or self._arrangement_assignment is None:
            raise RuntimeError("not in arrangement mode")
        if value not in self._arrangement_pool:
            raise PoolValueNotPresentError(f"value {value} not in pool {self._arrangement_pool}")
        existing = self._arrangement_assignment.get(stat_name)
        if existing is not None:
            self._arrangement_pool.append(existing)
        self._arrangement_pool.remove(value)
        self._arrangement_assignment[stat_name] = value

    def clear_stat(self, stat_name: str) -> None:
        """Return the value in ``stat_name`` (if any) to the pool."""
        if self._arrangement_pool is None or self._arrangement_assignment is None:
            raise RuntimeError("not in arrangement mode")
        existing = self._arrangement_assignment.get(stat_name)
        if existing is None:
            return
        self._arrangement_pool.append(existing)
        self._arrangement_assignment[stat_name] = None

    def confirm_arrangement(self) -> None:
        """Lock the arrangement and materialize ``rolled_stats``.

        Raises:
            UnfilledArrangementError: not all six slots filled.
            NoQualifyingClassesError: arrangement qualifies for zero classes.
        """
        if self._arrangement_assignment is None:
            raise RuntimeError("not in arrangement mode")
        if any(v is None for v in self._arrangement_assignment.values()):
            raise UnfilledArrangementError("not all six stats assigned")
        if not self._classes:
            raise RuntimeError("no classes attached; call with_classes() before confirm")
        qualifying = qualifying_classes_arrangement(
            self._arrangement_assignment,
            self._classes,
        )
        if not qualifying:
            raise NoQualifyingClassesError(
                f"arrangement {self._arrangement_assignment} qualifies for no class"
            )
        self._rolled_stats = [
            (name, self._arrangement_assignment[name]) for name in self._ability_score_names
        ]
        self._arrangement_pool = None
        self._arrangement_assignment = None

    def reject_arrangement(self) -> None:
        """Discard the current pool and reroll. Stays in arrange mode."""
        if self._arrangement_assignment is None:
            raise RuntimeError("not in arrangement mode")
        self._roll_3d6_arrange_visible()

    @property
    def rules(self) -> RulesConfig:
        """The full RulesConfig the builder was constructed with.

        Exposed so external renderers (chargen_summary) can read pack-
        wide vocabulary fields (``chargen_field_labels``) without
        having to thread the GenrePack rules separately.
        """
        return self._rules

    def race_label(self) -> str:
        """Genre-specific label for the "race" field (e.g., "Species", "Origin")."""
        return self._race_label

    def class_label(self) -> str:
        """Genre-specific label for the "class" field (e.g., "Archetype", "Path")."""
        return self._class_label

    def default_class(self) -> str | None:
        """Default class from the genre pack's rules, if defined.

        Used by external renderers to resolve starting equipment when
        chargen doesn't set an explicit class_hint.
        """
        return self._default_class

    def _name_scene_inputs(self) -> tuple[str | None, str | None]:
        """(original_freeform, followup_correction) from the name-entry scene.

        The name scene is the terminal name-entry scene (no choices AND
        ``allows_freeform`` — see ``_is_name_scene``); once answered, its
        result is the last result. Returns (None, None) when there is no name
        scene (e.g. heavy_metal & siblings end on a no-choice *display*
        confirmation scene with ``allows_freeform: false``, which is NOT a
        name scene) or it hasn't been answered with freeform text.
        """
        if not self._scenes:
            return (None, None)
        if not self._is_name_scene(len(self._scenes) - 1):
            return (None, None)
        if not self._results:
            return (None, None)
        last_result = self._results[-1]
        if not isinstance(last_result.input_type, FreeformInput):
            return (None, None)
        return (last_result.input_type.text, last_result.followup_text)

    def character_name(self) -> str | None:
        """Extract the character name from the name-entry scene.

        The name scene is the last scene with no choices. The freeform
        answer is parsed via ``extract_freeform_names`` (playtest 2026-06-05
        RW-2: "They call me Zeppo. The rig is Duck Soup — …" must yield
        "Zeppo", not the whole sentence), with the hook_prompt followup
        correction winning per-field over the original answer. When neither
        parses, the legacy verbatim text is kept (correction first — the
        player's latest word) so a wrong-but-present name beats a blank one.
        Blank text falls through to None so callers can substitute the
        lobby name.
        """
        original, correction = self._name_scene_inputs()
        if original is None:
            return None
        base_name, _ = extract_freeform_names(original)
        corr_name: str | None = None
        if correction is not None:
            corr_name, _ = extract_freeform_names(correction)
        name = corr_name or base_name
        if name:
            return name
        fallback = (correction or "").strip() or original.strip()
        return fallback if fallback else None

    def vessel_name(self) -> str | None:
        """The player-given vessel/rig name from the name-entry scene, if any.

        The second half of a two-part name question ("What do they call you?
        And what do they call the rig?"). Same parse-and-merge rules as
        ``character_name()``; no verbatim fallback — an unparsed vessel half
        is simply absent.
        """
        original, correction = self._name_scene_inputs()
        if original is None:
            return None
        _, base_vessel = extract_freeform_names(original)
        corr_vessel: str | None = None
        if correction is not None:
            _, corr_vessel = extract_freeform_names(correction)
        return corr_vessel or base_vessel

    # --- Accumulated view ---

    def accumulated(self) -> AccumulatedChoices:
        """Compute accumulated choices from scene results.

        Most hint fields follow last-one-wins (a later scene overrides
        an earlier one). Lists and stat_bonuses accumulate additively.

        ``reputation_bonus`` is accepted as pass-through on
        ``MechanicalEffects`` and accumulated here last-one-wins like
        other single-value hints.

        The pronoun-only-choice filter for ``backstory_fragments``
        excludes "He.", "She.", etc. — single-token pronoun picks that
        aren't narrative-bearing. Any other hint field on the same
        result re-qualifies the fragment so meaningful descriptions
        like "the armed woman with murder in her eyes" survive
        (reviewer finding from story 31-2).
        """
        acc = AccumulatedChoices()
        for result in self._results:
            eff = result.effects_applied

            # Single-value hints — last one wins.
            if eff.class_hint is not None:
                acc.class_hint = eff.class_hint
                # Capture the chosen vocation LABEL when the class was picked
                # from a choice button (e.g. "Country Veterinary Surgeon" →
                # class_hint "Doctor"). Display-only; symmetric with
                # background_label. Lets the summary + {class} prose show the
                # flavor instead of the collapsed archetype slug.
                #
                # BUT not when the SAME choice also sets race_hint: that's a
                # combined ORIGIN choice (elemental_harmony "The Ember Isles" →
                # race_hint "Ember Isles" + class_hint "Channeler"), whose label
                # names the origin, not the class. Capturing it here duplicated
                # origin_label onto calling_label and seeded a hollow quest
                # ("The Ember Isles · The Ember Isles"). The label belongs to
                # race_label (set below); calling_label resolves from class_hint.
                # ...but only when the label reads like a vocation. An oblique
                # flavor phrase that leads with an article ("A craft that costs
                # the craftsman", "The Gun") is NOT a job title; capturing it
                # produced the doubled-article Calling bug ("a A craft …").
                # Leaving class_label empty falls back to class_hint at build
                # time (the resolved class — "Elementalist", "Gunslinger").
                if (
                    result.choice_label is not None
                    and eff.race_hint is None
                    and _is_vocation_label(result.choice_label)
                ):
                    acc.class_label = result.choice_label
            # Freeform vocation display label (class-selecting scene answered
            # with free text). Last-wins, display-only.
            if result.freeform_class_label is not None:
                acc.class_label = result.freeform_class_label
            if eff.race_hint is not None:
                acc.race_hint = eff.race_hint
                # Capture the chosen origin LABEL when picked from a choice
                # button (e.g. "The Village Itself" → race_hint "Servant").
                # Display-only; the mechanical Character.race still resolves
                # from race_hint.
                # ...but not an indefinite-article descriptor phrase ("A Green
                # Martian of the Hordes") — leaving race_label empty falls the
                # sheet back to the resolved race_hint ("Green Martian"). The
                # race-axis sibling of the _is_vocation_label Calling guard
                # above (sq-playtest 2026-06-10, barsoom Tarkas).
                if result.choice_label is not None and _is_origin_display_label(
                    result.choice_label
                ):
                    acc.race_label = result.choice_label
            if eff.personality_trait is not None:
                acc.personality_trait = eff.personality_trait
            if eff.affinity_hint is not None:
                acc.affinity_hint = eff.affinity_hint
            if eff.background is not None:
                acc.background = eff.background
                if result.choice_label is not None:
                    acc.background_label = result.choice_label
            if eff.mutation_hint is not None:
                acc.mutation_hint = eff.mutation_hint
            if eff.origin_trait is not None:
                acc.origin_trait = eff.origin_trait
            if eff.training_hint is not None:
                acc.training_hint = eff.training_hint
            if eff.emotional_state is not None:
                acc.emotional_state = eff.emotional_state
            if eff.relationship is not None:
                acc.relationship = eff.relationship
            if eff.goals is not None:
                acc.goals = eff.goals
            if eff.rig_type_hint is not None:
                acc.rig_type_hint = eff.rig_type_hint
            if eff.rig_trait is not None:
                acc.rig_trait = eff.rig_trait
            if eff.catch_phrase is not None:
                acc.catch_phrase = eff.catch_phrase
            if eff.pronoun_hint is not None:
                acc.pronoun_hint = eff.pronoun_hint
            if eff.jungian_hint is not None:
                acc.jungian_hint = eff.jungian_hint
            if eff.rpg_role_hint is not None:
                acc.rpg_role_hint = eff.rpg_role_hint
            # Phase 1 IOU — spaghetti_western chargen-choice reputation tag.
            if eff.reputation_bonus is not None:
                acc.reputation_bonus = eff.reputation_bonus

            # Backstory-hook detection. A scene's effects look "drive-shaped"
            # when it touches the inner-life triplet (relationship / goals /
            # emotional_state) WITHOUT also setting an origin/profession-shape
            # field (race_hint / class_hint / mutation_hint / rig_type_hint).
            # space_opera's `drive` scene and tea_and_murder's `drive` scene match;
            # mutant_wasteland's `origins` (which sets race+background) does
            # NOT match — that genre's `background` IS the meaningful label
            # and stays the preview's source. Last-wins.
            looks_like_drive = (
                eff.relationship is not None
                or eff.goals is not None
                or eff.emotional_state is not None
            ) and not (
                eff.race_hint is not None
                or eff.class_hint is not None
                or eff.mutation_hint is not None
                or eff.rig_type_hint is not None
            )
            if looks_like_drive and result.choice_label is not None:
                acc.backstory_label = result.choice_label

            # Multi-value accumulation — item_hints skips sentinel "none"
            # and empty strings.
            if eff.item_hint is not None and eff.item_hint not in ("", "none"):
                acc.item_hints.append(eff.item_hint)

            # Backstory fragment collection with pronoun-only filter.
            if result.choice_description is not None:
                is_pronoun_only = eff.pronoun_hint is not None and all(
                    v is None
                    for v in (
                        eff.class_hint,
                        eff.race_hint,
                        eff.mutation_hint,
                        eff.item_hint,
                        eff.affinity_hint,
                        eff.training_hint,
                        eff.background,
                        eff.personality_trait,
                        eff.emotional_state,
                        eff.relationship,
                        eff.goals,
                        eff.rig_type_hint,
                        eff.rig_trait,
                        eff.catch_phrase,
                    )
                )
                if not is_pronoun_only:
                    acc.backstory_fragments.append(result.choice_description)

            # Stat bonuses accumulate additively across all scenes.
            for stat, bonus in eff.stat_bonuses.items():
                acc.stat_bonuses[stat] = acc.stat_bonuses.get(stat, 0) + bonus

        return acc

    # --- Protocol rendering ---

    def interpolate_scene_narration(self, text: str) -> str:
        """Resolve {name}/{class}/{race} placeholders in scene narration.

        Resolution order for {name}: the player's scene-entered name wins, falling
        back to the lobby name for genres that don't include a name-entry scene.
        This matches render_confirmation_summary's name resolution.

        OTEL watcher events:
          - ``chargen.scene_narration_interpolated`` emitted when at least one of
            the three recognized tokens was present. Attributes record which
            tokens appeared and whether each resolved to a non-empty string. The
            event carries ``severity=warn`` when any present token resolved
            empty (class_hint not set before a scene that templates {class}),
            otherwise ``severity=info``.
          - ``chargen.scene_narration_unrecognized_placeholder`` emitted once per
            unrecognized ``{...}`` token left in the rendered output. One event
            per offending token surfaces all typos, not just the leftmost
            (SOUL.md: no silent fallbacks).
        """
        if "{" not in text:
            return text

        acc = self.accumulated()
        name = self.character_name() or self._lobby_name or ""
        # Freeform vocation label (player's own words) wins for the prose slot;
        # canned classes fall through to class_hint.
        class_ = acc.class_label or acc.class_hint or ""
        # {race} prose intentionally uses the mechanical hint, NOT race_label:
        # templates phrase it as a noun ("come up from a {race} household"), so
        # the archetype slug ("Servant") fits grammatically where an origin
        # flavor label ("The Village Itself") would not. The chargen-summary
        # FIELD uses race_label (display-only); the prose slot keeps the hint.
        race = acc.race_hint or ""

        had_name = "{name}" in text
        had_class = "{class}" in text
        had_race = "{race}" in text

        span = trace.get_current_span()

        if had_name or had_class or had_race:
            rendered = (
                text.replace("{name}", name).replace("{class}", class_).replace("{race}", race)
            )
            any_empty = (
                (had_name and not name) or (had_class and not class_) or (had_race and not race)
            )
            attrs: dict[str, object] = {
                "action": "scene_narration_interpolated",
                "severity": "warn" if any_empty else "info",
            }
            if had_name:
                attrs["name_resolved"] = bool(name)
            if had_class:
                attrs["class_resolved"] = bool(class_)
            if had_race:
                attrs["race_resolved"] = bool(race)
            span.add_event("chargen.scene_narration_interpolated", attrs)
        else:
            rendered = text

        for unrecognized in find_unrecognized_tokens(rendered):
            span.add_event(
                "chargen.scene_narration_unrecognized_placeholder",
                {
                    "action": "scene_narration_unrecognized_placeholder",
                    "token": unrecognized,
                    "severity": "warn",
                },
            )

        return rendered

    def to_scene_message(self, player_id: str) -> CharacterCreationMessage:
        """Render the current builder phase as a CHARACTER_CREATION message.

        Covers InProgress and AwaitingFollowup. Confirmation-phase
        rendering requires pack inventory + the lobby-provided name,
        neither of which the builder owns; the server's
        ``chargen_summary`` module renders confirmation from the outside
        via ``render_confirmation_summary``. Calling this method in
        Confirmation phase is a programmer error and raises
        ``RuntimeError`` with a diagnostic.

        Wire format notes:
          - scene_index is 0-based on the wire. The payload docstring
            calls it "1-based" — that's a pre-existing mislabel; UI
            consumers already display ``scene_index + 1``.
          - Empty label/description on any CharCreationChoice fails loud via
            the NonBlankString validator — pack YAML must fix blanks at the
            source, not silently fall back at render time.
          - rolled_stats is only populated when the current scene declares
            stat_generation in its mechanical_effects. The UI renders rolled
            stats as a structured stat block; the narration text stays clean
            (no inline "**STR 10** · **DEX 13** · ..." parsing on the client).
          - Display-only scenes (empty choices, allows_freeform=False) emit
            input_type="continue" with allows_freeform=False. Name-entry scenes
            (empty choices, allows_freeform=True) emit input_type="name" with
            allows_freeform=True. Choice scenes pass through scene.allows_freeform.
        """
        match self._phase:
            case InProgress(scene_index=scene_index):
                scene = self._filter_class_choices(self._scenes[scene_index])
                eff = scene.mechanical_effects

                # the_arrangement — assignment-required scenes get a custom payload.
                if eff is not None and eff.assignment_required:
                    return self._render_arrangement_message(scene, scene_index, player_id)

                # the_story — identity-capture scenes get a custom payload.
                if eff is not None and eff.identity_capture is not None:
                    return self._render_story_message(scene, scene_index, player_id)

                choices = [
                    CreationChoice(
                        label=NonBlankString(c.label),
                        description=NonBlankString(c.description),
                    )
                    for c in scene.choices
                ]

                scene_allows_freeform = bool(scene.allows_freeform)
                if not choices:
                    if scene_allows_freeform:
                        input_type = "name"
                        allows_freeform: bool | None = True
                    else:
                        input_type = "continue"
                        allows_freeform = False
                else:
                    input_type = "choice"
                    allows_freeform = scene.allows_freeform

                scene_has_stat_gen = (
                    scene.mechanical_effects is not None
                    and scene.mechanical_effects.stat_generation is not None
                )
                rolled_stats_payload: list[RolledStat] | None = None
                if scene_has_stat_gen and self._rolled_stats is not None:
                    rolled_stats_payload = [
                        RolledStat(name=ability, value=value)
                        for ability, value in self._rolled_stats
                    ]

                payload = CharacterCreationPayload(
                    phase="scene",
                    scene_index=scene_index,
                    total_scenes=len(self._scenes),
                    prompt=self.interpolate_scene_narration(scene.narration),
                    choices=choices,
                    allows_freeform=allows_freeform,
                    input_type=input_type,
                    loading_text=scene.loading_text,
                    rolled_stats=rolled_stats_payload,
                )
                return CharacterCreationMessage(payload=payload, player_id=player_id)

            case AwaitingFollowup(hook_prompt=hook_prompt):
                payload = CharacterCreationPayload(
                    phase="scene",
                    scene_index=None,
                    total_scenes=len(self._scenes),
                    prompt=hook_prompt,
                    allows_freeform=True,
                    input_type="text",
                )
                return CharacterCreationMessage(payload=payload, player_id=player_id)

            case Confirmation():
                raise RuntimeError(
                    "CharacterBuilder.to_scene_message called in Confirmation phase. "
                    "Callers must branch on is_confirmation() and invoke "
                    "sidequest.server.dispatch.chargen_summary.render_confirmation_summary "
                    "instead. The builder cannot render a complete summary without pack "
                    "inventory and the lobby-provided name."
                )

            case _:  # pragma: no cover — exhaustive
                raise AssertionError(f"unknown phase: {self._phase!r}")

    def _render_arrangement_message(
        self,
        scene: CharCreationScene,
        scene_index: int,
        player_id: str,
    ) -> CharacterCreationMessage:
        """Render the_arrangement scene payload with pool/assignment/qualify state."""
        pool = list(self._arrangement_pool) if self._arrangement_pool is not None else []
        assignment: dict[str, int | None] = (
            dict(self._arrangement_assignment)
            if self._arrangement_assignment is not None
            else {name: None for name in self._ability_score_names}
        )
        qualifying = qualifying_classes_arrangement(assignment, self._classes)
        qualifying_names = [c.display_name for c in qualifying]
        class_requirements = [
            ClassRequirement(
                name=c.display_name,
                requirement_label=f"{c.prime_requisite} {c.minimum_score}+",
            )
            for c in self._classes
        ]
        all_filled = bool(assignment) and all(v is not None for v in assignment.values())
        confirm_enabled = all_filled and bool(qualifying)
        payload = CharacterCreationPayload(
            phase="scene",
            scene_index=scene_index,
            total_scenes=len(self._scenes),
            prompt=self.interpolate_scene_narration(scene.narration),
            input_type="stat_arrange",
            loading_text=scene.loading_text,
            pool=pool,
            assignment=assignment,
            qualifying_classes=qualifying_names,
            class_requirements=class_requirements,
            confirm_enabled=confirm_enabled,
        )
        return CharacterCreationMessage(payload=payload, player_id=player_id)

    def _render_story_message(
        self,
        scene: CharCreationScene,
        scene_index: int,
        player_id: str,
    ) -> CharacterCreationMessage:
        """Render the_story scene payload with pronouns/background/description fields."""
        eff = scene.mechanical_effects
        ic = eff.identity_capture if eff is not None else None
        autogen_available = bool(eff is not None and eff.background_autogen_source is not None)
        payload = CharacterCreationPayload(
            phase="scene",
            scene_index=scene_index,
            total_scenes=len(self._scenes),
            prompt=self.interpolate_scene_narration(scene.narration),
            input_type="story",
            loading_text=scene.loading_text,
            pronouns_options=["she/her", "he/him", "they/them"],
            pronouns_allow_freeform=True,
            background_optional=ic.background_optional if ic is not None else True,
            description_optional=ic.description_optional if ic is not None else True,
            autogen_available=autogen_available,
        )
        return CharacterCreationMessage(payload=payload, player_id=player_id)

    # --- Actions: scene walking ---

    def apply_choice(self, index: int) -> None:
        """Apply a numbered choice to the current scene.

        Raises WrongPhaseError if not in InProgress, InvalidChoiceError if
        index is out of range.

        Transitions: if the scene has a hook_prompt, moves to
        AwaitingFollowup; otherwise advances to the next scene (or
        Confirmation if this was the last scene).
        """
        match self._phase:
            case InProgress(scene_index=scene_index):
                pass
            case AwaitingFollowup():
                raise WrongPhaseError(expected="InProgress", actual="AwaitingFollowup")
            case Confirmation():
                raise WrongPhaseError(expected="InProgress", actual="Confirmation")
            case _:  # pragma: no cover
                raise AssertionError(f"unknown phase: {self._phase!r}")

        # Filter class-scene choices the same way current_scene() and the
        # wire protocol do — apply_choice's `index` is the user-facing
        # index, so it must read from the same filtered view.
        scene = self._filter_class_choices(self._scenes[scene_index])
        if index >= len(scene.choices):
            # Saturating subtraction so an empty-choice scene reports
            # ``max_index=0`` instead of a negative value.
            max_index = max(len(scene.choices) - 1, 0)
            raise InvalidChoiceError(index=index, max_index=max_index)

        choice = scene.choices[index]
        effects = choice.mechanical_effects
        hooks = extract_hooks(scene.id, effects)
        anchors = extract_anchors(scene.id, effects)

        self._results.append(
            SceneResult(
                input_type=ChoiceInput(index=index),
                effects_applied=effects,
                hooks_added=hooks,
                anchors_added=anchors,
                choice_description=choice.description,
                choice_label=choice.label,
            )
        )

        if effects.class_hint is not None:
            trace.get_current_span().add_event(
                "chargen.class_chosen",
                {"class_hint": effects.class_hint},
            )

        if scene.hook_prompt is not None:
            self._phase = AwaitingFollowup(
                scene_index=scene_index,
                hook_prompt=scene.hook_prompt,
            )
        else:
            self._advance_scene(scene_index)

    def apply_freeform(self, text: str) -> None:
        """Apply freeform text input to the current scene.

        Allowed when `scene.allows_freeform` is True OR the scene has no
        choices (name-entry scenes at the end of chargen). Raises
        FreeformNotAllowedError otherwise.

        Uses scene-level `mechanical_effects` if present (e.g. name/stat
        scenes declaring stat_generation or equipment_generation). The
        actual stat roll re-execution based on scene directives lands
        in Slice 3; Slice 2 records the effects but does not re-roll.
        """
        if not isinstance(self._phase, InProgress):
            raise WrongPhaseError(expected="InProgress", actual=self._phase_name())
        scene_index = self._phase.scene_index
        scene = self._scenes[scene_index]

        # Allow freeform only when the scene explicitly allows it, OR when
        # the scene has no choices (name-entry scenes at the end of chargen).
        if not scene.allows_freeform and scene.choices:
            raise FreeformNotAllowedError()

        # Use scene-level mechanical_effects if present, otherwise empty.
        effects = (
            scene.mechanical_effects
            if scene.mechanical_effects is not None
            else MechanicalEffects()
        )

        # Scene-level stat_generation directive applies at freeform
        # input: roll_3d6_strict re-rolls; any other method overrides
        # the builder's default stat_generation for later
        # generate_stats() calls.
        if effects.stat_generation is not None:
            if effects.stat_generation == "roll_3d6_strict":
                self._roll_3d6_strict()
            elif effects.stat_generation == "roll_3d6_arrange_visible":
                # Scene-flow method, not a generate_stats method.
                # confirm_arrangement materializes _rolled_stats.
                pass
            else:
                self._stat_generation = effects.stat_generation

        hooks = extract_hooks(scene.id, effects)
        anchors = extract_anchors(scene.id, effects)

        # Class-selecting scene answered with free text: every canned choice
        # carries a class_hint, but the freeform answer carries none. Capture
        # the player's words as a display-only label for the {class} prose
        # slot — the mechanical char_class still resolves via class_hint or the
        # pack default, so the starting-loadout class match doesn't regress.
        freeform_class_label: str | None = None
        is_class_scene = bool(scene.choices) and all(
            c.mechanical_effects.class_hint for c in scene.choices
        )
        if is_class_scene:
            derived = derive_class_label(text)
            if derived:
                freeform_class_label = derived
                trace.get_current_span().add_event(
                    "chargen.freeform_class_label_derived",
                    {
                        "action": "freeform_class_label_derived",
                        "scene_id": scene.id,
                        "label": derived,
                        "severity": "info",
                    },
                )

        self._results.append(
            SceneResult(
                input_type=FreeformInput(text=text),
                effects_applied=effects,
                hooks_added=hooks,
                anchors_added=anchors,
                choice_description=None,
                freeform_class_label=freeform_class_label,
                # Story 93-1: stamp the source scene so freeform_answer_texts()
                # can exclude the name-entry scene from the archetype-inference
                # fodder without re-deriving result→scene alignment.
                scene_id=scene.id,
            )
        )

        # Playtest 2026-06-05 (RW-2): the name-entry scene (last scene, no
        # choices) parses the freeform answer into name + vessel halves.
        # Emit the extraction decision so the GM panel can see what the
        # parser did with the player's words (the reported OTEL gap: "no
        # extraction span fired on either submit").
        if self._is_name_scene(scene_index):
            extracted_name, extracted_vessel = extract_freeform_names(text)
            trace.get_current_span().add_event(
                "chargen.names_extracted",
                {
                    "action": "names_extracted",
                    "scene_id": scene.id,
                    "raw_len": len(text),
                    "extracted_name": extracted_name or "",
                    "extracted_vessel_name": extracted_vessel or "",
                    "fallback_verbatim": extracted_name is None,
                    "severity": "info" if extracted_name else "warn",
                },
            )

        if scene.hook_prompt is not None:
            self._phase = AwaitingFollowup(
                scene_index=scene_index,
                hook_prompt=scene.hook_prompt,
            )
        else:
            self._advance_scene(scene_index)

    def freeform_answer_texts(self) -> list[str]:
        """The player's freeform scene answers — archetype-inference fodder.

        Story 93-1: when the archetype gate would block with
        ``missing_axes_with_pack_axes``, the confirm seam infers the missing
        axes from these texts. Name-entry scene answers are EXCLUDED: every
        player types a name (preset-only players included), so counting the
        name would make the no-freeform fail-loud path unreachable and spend
        a Haiku call on text with no archetype signal.

        Derived from ``_results`` (revert-safe — a popped result drops its
        text) using the ``scene_id`` stamped by ``apply_freeform``. Results
        from other input paths that left ``scene_id`` as ``None`` are
        included: only a positively-identified name scene is excluded.
        """
        name_scene_ids = {
            self._scenes[i].id for i in range(len(self._scenes)) if self._is_name_scene(i)
        }
        return [
            result.input_type.text
            for result in self._results
            if isinstance(result.input_type, FreeformInput)
            and result.input_type.text.strip()
            and (result.scene_id is None or result.scene_id not in name_scene_ids)
        ]

    def _is_name_scene(self, scene_index: int) -> bool:
        """True when ``scene_index`` is the name-entry scene: the terminal
        scene with no choices AND ``allows_freeform`` set.

        ``allows_freeform`` is the load-bearing discriminator. A name-entry
        scene (road_warrior's ``the_name``) has no choices and
        ``allows_freeform: true`` — it renders ``input_type="name"`` (see
        ``to_scene_message``). A terminal *display*/confirmation scene
        (heavy_metal and 8 sibling packs) also has no choices but
        ``allows_freeform: false`` — it renders ``input_type="continue"`` and
        is never answered with a name. Treating the latter as a name scene
        leaked the prior scene's freeform answer into ``character_name()``
        and the confirmation prose's ``{name}`` slot ([BAR-1])."""
        scene = self._scenes[scene_index]
        return (
            scene_index == len(self._scenes) - 1
            and not scene.choices
            and bool(scene.allows_freeform)
        )

    def answer_followup(self, text: str) -> None:
        """Answer a followup prompt while in AwaitingFollowup state.

        Inserts a Wound hook at position 0 of the most recent result — the
        followup answer is the player's primary hook (trauma description,
        motive elaboration, backstory beat). Advances to the next scene
        (or Confirmation).

        Name-scene exception (playtest 2026-06-05 RW-2): when the followup
        belongs to the name-entry scene, the answer is the player's NAME
        CORRECTION ("Road name: Zeppo. Rig name: Duck Soup."), not a trauma
        hook. It's stored on the result's ``followup_text`` so
        ``character_name()``/``vessel_name()`` re-parse it (correction wins
        per-field) — previously the correction was buried as a WOUND hook
        and the re-prompt was a dead input.
        """
        if not isinstance(self._phase, AwaitingFollowup):
            raise WrongPhaseError(expected="AwaitingFollowup", actual=self._phase_name())
        scene_index = self._phase.scene_index
        scene_id = self._scenes[scene_index].id

        if self._is_name_scene(scene_index) and self._results:
            self._results[-1].followup_text = text
            corr_name, corr_vessel = extract_freeform_names(text)
            trace.get_current_span().add_event(
                "chargen.name_followup_correction",
                {
                    "action": "name_followup_correction",
                    "scene_id": scene_id,
                    "extracted_name": corr_name or "",
                    "extracted_vessel_name": corr_vessel or "",
                    "severity": "info" if (corr_name or corr_vessel) else "warn",
                },
            )
        elif self._results:
            # Insert the followup hook at position 0 on the most recent result.
            self._results[-1].hooks_added.insert(
                0,
                NarrativeHook(
                    hook_type=HookType.WOUND,
                    source_scene=scene_id,
                    text=text,
                    mechanical_key=None,
                ),
            )

        self._advance_scene(scene_index)

    def apply_auto_advance(self) -> None:
        """Auto-advance a display-only scene (no choices, no freeform).

        For scenes that narrate and wait for the player's Continue ack.
        Applies scene-level mechanical_effects and advances. Raises
        InvalidChoiceError if the scene requires input.

        Slice 2 records the effects but does not re-execute stat rolling
        — that lands in Slice 3.
        """
        if not isinstance(self._phase, InProgress):
            raise WrongPhaseError(expected="InProgress", actual=self._phase_name())
        scene_index = self._phase.scene_index
        scene = self._scenes[scene_index]

        if scene.choices or scene.allows_freeform:
            raise InvalidChoiceError(index=0, max_index=len(scene.choices))

        effects = (
            scene.mechanical_effects
            if scene.mechanical_effects is not None
            else MechanicalEffects()
        )

        # Scene-level stat_generation directive: roll_3d6_strict only
        # rolls if we don't already have rolled stats (unlike
        # apply_freeform which unconditionally re-rolls). auto_advance
        # guards on ``rolled_stats is None``; apply_freeform always
        # re-rolls.
        if effects.stat_generation is not None:
            if effects.stat_generation == "roll_3d6_strict":
                if self._rolled_stats is None:
                    self._roll_3d6_strict()
                elif self._classes and self._rolled_stats is not None:
                    # Stats already rolled (eager construction roll fired before
                    # with_classes() was called). Emit class_qualifying now that
                    # classes are available so the GM panel can see which
                    # classes the player qualifies for.
                    qual = qualifying_classes(dict(self._rolled_stats), self._classes)
                    trace.get_current_span().add_event(
                        "chargen.class_qualifying",
                        {"class_ids": [c.id for c in qual]},
                    )
            elif effects.stat_generation == "roll_3d6_arrange_visible":
                # The pool was rolled at construction; arrangement
                # materializes _rolled_stats. generate_stats() reuses the
                # ``roll_3d6_strict`` branch — both materialize stats
                # before generate_stats runs, so don't override
                # ``self._stat_generation`` with the scene-flow string.
                pass
            else:
                self._stat_generation = effects.stat_generation

        self._results.append(
            SceneResult(
                input_type=ChoiceInput(index=0),
                effects_applied=effects,
                hooks_added=[],
                anchors_added=[],
                choice_description=None,
            )
        )

        self._advance_scene(scene_index)

    def apply_response(self, response: SceneInputType) -> None:
        """Unified dispatcher for ChoiceInput / FreeformInput.

        Routes ``ChoiceInput`` to :meth:`apply_choice` and ``FreeformInput``
        to :meth:`apply_freeform`. Guards against the arrangement scene:
        when the current scene's ``mechanical_effects.assignment_required``
        is True, neither input variant is valid — callers must use
        :meth:`apply_arrangement_confirm` / :meth:`apply_arrangement_reject`.
        """
        if isinstance(self._phase, InProgress):
            scene = self._scenes[self._phase.scene_index]
            eff = scene.mechanical_effects
            if eff is not None and eff.assignment_required:
                raise ArrangementSceneActiveError(
                    f"scene {scene.id!r} requires "
                    f"apply_arrangement_confirm/reject, not apply_response"
                )
        if isinstance(response, StoryInput):
            self._apply_story(response)
        elif isinstance(response, ChoiceInput):
            self.apply_choice(response.index)
        elif isinstance(response, FreeformInput):
            self.apply_freeform(response.text)
        else:  # pragma: no cover
            raise TypeError(f"unknown SceneInputType: {type(response).__name__}")

    def apply_arrangement_confirm(self) -> None:
        """Confirm the arrangement, materialize ``rolled_stats``, and advance.

        Records a SceneResult mirroring the scene's mechanical_effects so
        ``go_back`` parity holds. Raises ``UnfilledArrangementError`` /
        ``NoQualifyingClassesError`` from :meth:`confirm_arrangement` if
        the arrangement is incomplete or qualifies for no class.
        """
        if not isinstance(self._phase, InProgress):
            raise WrongPhaseError(expected="InProgress", actual=self._phase_name())
        scene_index = self._phase.scene_index
        scene = self._scenes[scene_index]
        eff = scene.mechanical_effects
        if not (eff is not None and eff.assignment_required):
            raise RuntimeError(f"scene {scene.id!r} is not an arrangement scene")
        # confirm_arrangement may raise UnfilledArrangementError /
        # NoQualifyingClassesError — let them propagate.
        self.confirm_arrangement()

        self._results.append(
            SceneResult(
                input_type=ChoiceInput(index=0),
                effects_applied=eff,
                hooks_added=[],
                anchors_added=[],
                choice_description=None,
            )
        )
        self._advance_scene(scene_index)

    def apply_arrangement_reject(self) -> None:
        """Reject the current pool, reroll, and stay on the arrangement scene."""
        if not isinstance(self._phase, InProgress):
            raise WrongPhaseError(expected="InProgress", actual=self._phase_name())
        scene_index = self._phase.scene_index
        scene = self._scenes[scene_index]
        eff = scene.mechanical_effects
        if not (eff is not None and eff.assignment_required):
            raise RuntimeError(f"scene {scene.id!r} is not an arrangement scene")
        self.reject_arrangement()

    def _apply_story(self, response: StoryInput) -> None:
        """Apply a StoryInput to the_story scene.

        Records pronouns into ``MechanicalEffects.pronoun_hint`` (matching
        the existing pronouns-scene channel) and folds the freeform
        background + description text into ``MechanicalEffects.background``
        (matching the existing backstory-scene channel). When
        ``identity_capture.pronouns_required`` is True, blank/whitespace
        pronouns raise ``UnfilledArrangementError``.

        Description and background are joined with a separator since
        ``MechanicalEffects`` has no dedicated ``description`` field;
        the combined text lands in ``background`` so downstream
        AccumulatedChoices.background pickup is unchanged. Empty
        components are dropped from the join.
        """
        if not isinstance(self._phase, InProgress):
            raise WrongPhaseError(expected="InProgress", actual=self._phase_name())
        scene_index = self._phase.scene_index
        scene = self._scenes[scene_index]
        scene_eff = scene.mechanical_effects

        # Gate: pronouns_required.
        identity = scene_eff.identity_capture if scene_eff is not None else None
        pronouns_required = identity.pronouns_required if identity is not None else False
        pronouns = response.pronouns.strip()
        if pronouns_required and not pronouns:
            raise UnfilledArrangementError("pronouns required")

        # Compose the scene's recorded effects: pronoun_hint + background.
        background_parts = [
            response.background.strip(),
            response.description.strip(),
        ]
        joined_background = " | ".join(p for p in background_parts if p)

        effects = MechanicalEffects(
            pronoun_hint=pronouns or None,
            background=joined_background or None,
        )

        hooks = extract_hooks(scene.id, effects)
        anchors = extract_anchors(scene.id, effects)

        self._results.append(
            SceneResult(
                input_type=response,
                effects_applied=effects,
                hooks_added=hooks,
                anchors_added=anchors,
                choice_description=None,
                scene_id=scene.id,
            )
        )

        self._advance_scene(scene_index)

    def go_back(self) -> None:
        """Navigate backward, undoing the last scene result.

        Pops the most recent SceneResult and sets the phase back to that
        scene's index. Raises WrongPhaseError if there are no results to
        revert (we're at the first scene with no history).
        """
        if not self._results:
            raise WrongPhaseError(
                expected="InProgress with history",
                actual="no previous scenes to return to",
            )
        self._results.pop()
        target = len(self._results)
        self._phase = InProgress(scene_index=target)

    def revert(self) -> None:
        """Revert the last scene — pop the SceneResult and go back one.

        Distinct from ``go_back`` in that ``go_back``'s
        "at-the-first-scene" guard raises ``WrongPhaseError``; ``revert``
        raises ``CannotRevertError``. Callers depend on the specific
        error variant.
        """
        if not self._results:
            raise CannotRevertError()
        self._results.pop()
        self._phase = InProgress(scene_index=len(self._results))

    # --- Finalizer ---

    def build(self, name: str) -> Character:
        """Build the final Character from accumulated choices.

        Only valid from Confirmation phase — raises ``WrongPhaseError``
        otherwise. Composes the Character from accumulated hints:
        race/class (accumulated or rules default), stats (via
        ``generate_stats``), backstory (fragments OR tables OR
        mechanical labels OR hardcoded fallback), abilities (resolved
        from mutation / affinity / training hints with an AbilitySource
        tag), inventory (item_hints first then equipment_tables), edge
        pool (from edge_config OR placeholder for legacy packs), and
        the Fighter +2 Edge stub from Story 39-4.

        Numeric-name guard (Story 30-1): reject purely numeric names —
        they indicate a UI choice index leaked into the name fallback.
        Blank names are caught by the Character pydantic validators.

        OTEL watcher events are emitted via the current span's
        ``add_event`` API. Events carry structured attributes so the GM
        panel can reconstruct decisions: backstory method, equipment
        method, edge seeding source, etc. SOUL.md: no silent fallbacks
        — every path that resolves a default explicitly emits the
        fallback source and severity.
        """
        if not self.is_confirmation():
            raise WrongPhaseError(expected="Confirmation", actual=self._phase_name())

        # Numeric-name guard — a purely digit name is a UI index bleed.
        trimmed = name.strip()
        if trimmed and trimmed.isdigit():
            raise NumericNameError(name=trimmed)

        acc = self.accumulated()

        race_str = acc.race_hint or self._default_race or "Human"
        class_str = acc.class_hint or self._default_class or "Fighter"

        stats = self.generate_stats(acc)
        span = trace.get_current_span()

        # Hooks: collect narrative hooks, excluding mechanical traits
        # already represented on the sheet (race, class, personality).
        excluded_keys = {"race_hint", "class_hint", "personality_trait"}
        hooks: list[str] = []
        for result in self._results:
            for h in result.hooks_added:
                if h.mechanical_key is not None and h.mechanical_key in excluded_keys:
                    continue
                hooks.append(h.text)

        # Auto-fill lore anchors for faction / npc / location — if no
        # scene contributed an anchor of that type, note the gap so the
        # narrator (or the dispatch layer) can seed from the genre pack.
        anchor_types = ("faction", "npc", "location")
        for atype in anchor_types:
            has_anchor = any(a.anchor_type == atype for r in self._results for a in r.anchors_added)
            if not has_anchor:
                hooks.append(f"{atype}: auto-filled from genre pack")

        # Inventory composition: item_hints first, then equipment_tables
        # when a scene directive opts in (Story 31-3).
        items: list[dict] = []
        for i, hint in enumerate(acc.item_hints):
            id_str = hint.lower().replace(" ", "_") or f"item_{i}"
            display_name = humanize_snake_case(hint) or "Unknown Item"
            items.append(
                {
                    "id": id_str,
                    "name": display_name,
                    "description": f"Starting equipment: {display_name}",
                    "category": "weapon",
                    "value": 10,
                    "weight": 3.0,
                    "rarity": "common",
                    "narrative_weight": 0.3,
                    "tags": [],
                    "equipped": True,
                    "quantity": 1,
                    "uses_remaining": None,
                    "state": "Carried",
                }
            )

        random_table_requested = any(
            r.effects_applied.equipment_generation == "random_table" for r in self._results
        )
        class_kit_requested = any(
            r.effects_applied.equipment_generation == "class_kit" for r in self._results
        )

        # Resolve the kit_tables dict to roll from.  class_kit takes
        # precedence; random_table is the fallback for packs that don't
        # declare per-class kits.
        kit_tables: dict[str, list[str]] | None = None
        kit_source = "none"
        if class_kit_requested and self._equipment_tables is not None and self._classes:
            chosen_class = next(
                (c for c in self._classes if c.display_name == class_str),
                None,
            )
            if chosen_class is None:
                span.add_event(
                    "chargen.class_kit_unresolved",
                    {"class_str": class_str, "severity": "error"},
                )
            else:
                kit_tables = self._equipment_tables.class_tables.get(chosen_class.kit_table)
                kit_source = f"class_kit:{chosen_class.kit_table}"
                if kit_tables is None:
                    span.add_event(
                        "chargen.class_kit_table_missing",
                        {"kit_table": chosen_class.kit_table, "severity": "error"},
                    )

        # Existing random_table fallback path:
        if kit_tables is None and random_table_requested and self._equipment_tables is not None:
            kit_tables = self._equipment_tables.tables
            kit_source = "random_table"

        if kit_tables is not None and self._equipment_tables is not None:
            added = 0
            skipped = 0
            for slot, candidates in kit_tables.items():
                if not candidates:
                    continue
                rolls = self._equipment_tables.rolls_per_slot.get(slot, 1)
                for _ in range(rolls):
                    pick = candidates[self._rng.randrange(len(candidates))]
                    if not pick.strip():
                        # Blank id — surface the malformed content entry
                        # instead of silently producing a short inventory.
                        span.add_event(
                            "chargen.blank_item_id_skipped",
                            {"slot": slot, "pick": pick, "severity": "warn"},
                        )
                        skipped += 1
                        continue
                    display_name = humanize_snake_case(pick) or "Unknown Item"
                    items.append(
                        {
                            "id": pick,
                            "name": display_name,
                            "description": f"Starting equipment ({slot}): {display_name}",
                            "category": slot or "misc",
                            "value": 0,
                            "weight": 1.0,
                            "rarity": "common",
                            "narrative_weight": 0.3,
                            "tags": [],
                            "equipped": False,
                            "quantity": 1,
                            "uses_remaining": None,
                            "state": "Carried",
                        }
                    )
                    added += 1
            if class_kit_requested and kit_source.startswith("class_kit:"):
                span.add_event(
                    "chargen.class_kit_rolled",
                    {"kit_id": kit_source, "slot_count": len(kit_tables)},
                )
            equipment_method = kit_source
            equipment_added = added
            equipment_skipped = skipped
        elif random_table_requested and not class_kit_requested:
            # Directive present but no equipment_tables wired — this is
            # a misconfiguration, not graceful degradation. SOUL.md: no
            # silent fallbacks.
            span.add_event(
                "chargen.equipment_tables_missing",
                {
                    "reason": (
                        "scene declared `equipment_generation: random_table` "
                        "but CharacterBuilder has no equipment_tables wired"
                    ),
                    "severity": "warn",
                },
            )
            equipment_method = "none"
            equipment_added = 0
            equipment_skipped = 0
        elif class_kit_requested and self._equipment_tables is None:
            span.add_event(
                "chargen.equipment_tables_missing",
                {
                    "reason": (
                        "scene declared `equipment_generation: class_kit` "
                        "but CharacterBuilder has no equipment_tables wired"
                    ),
                    "severity": "warn",
                },
            )
            equipment_method = "none"
            equipment_added = 0
            equipment_skipped = 0
        else:
            equipment_method = "hints"
            equipment_added = 0
            equipment_skipped = 0

        span.add_event(
            "chargen.equipment_composed",
            {
                "method": equipment_method,
                "items_added": equipment_added,
                "items_skipped": equipment_skipped,
            },
        )

        # Backstory composition: fragments → tables → mechanical labels
        # → fallback. Every branch emits method + length so the GM
        # panel sees when a genre silently falls through to the
        # hardcoded "wanderer with a mysterious past" default.
        if acc.backstory_fragments:
            backstory_text = " ".join(acc.backstory_fragments)
            backstory_method = "fragments"
        elif self._backstory_tables is not None:
            tables = self._backstory_tables
            result = tables.template
            for key, entries in tables.tables.items():
                if entries:
                    pick = entries[self._rng.randrange(len(entries))]
                    result = result.replace(f"{{{key}}}", pick)
            backstory_text = strip_unmatched_placeholders(result)
            backstory_method = "tables"
        else:
            parts: list[str] = []
            if acc.background is not None:
                parts.append(f"Background: {acc.background}")
            if acc.personality_trait is not None:
                parts.append(f"Personality: {acc.personality_trait}")
            backstory_text = ". ".join(parts) if parts else "A wanderer with a mysterious past"
            backstory_method = "fallback"
        from sidequest.telemetry.spans import SPAN_CHARGEN_BACKSTORY_COMPOSED

        span.add_event(
            SPAN_CHARGEN_BACKSTORY_COMPOSED,
            {"method": backstory_method, "length": len(backstory_text)},
        )

        # Abilities: resolve from mutation / affinity / training hints.
        # Each hint type maps to an AbilitySource. The label and
        # description come from the scene choice the player selected.
        abilities: list[AbilityDefinition] = []
        for i, result in enumerate(self._results):
            eff = result.effects_applied
            hint_info: tuple[str, AbilitySource] | None = None
            if eff.mutation_hint is not None and eff.mutation_hint != "none":
                hint_info = (eff.mutation_hint, AbilitySource.Race)
            elif eff.affinity_hint is not None and eff.affinity_hint != "none":
                hint_info = (eff.affinity_hint, AbilitySource.Class)
            elif eff.training_hint is not None:
                hint_info = (eff.training_hint, AbilitySource.Class)

            if hint_info is None:
                continue

            hint_key, source = hint_info
            # Recover the label from the scene choice. Results are
            # ordered the same as scenes walked so index matches.
            label: str | None = None
            if i < len(self._scenes) and isinstance(result.input_type, ChoiceInput):
                scene = self._scenes[i]
                if result.input_type.index < len(scene.choices):
                    label = scene.choices[result.input_type.index].label
            if label is None:
                label = humanize_snake_case(hint_key)
            description = result.choice_description or (
                f"Acquired through character creation: {label}"
            )

            # Hint-based Class-source abilities have no known owning class —
            # they are scene-level training/affinity hints, not classes.yaml
            # signatures. reference_url_for_ability returns None when
            # owning_class_name is None, so these always get reference_url=None.
            # Per spec: only Class-source paths that *fail to resolve* emit a
            # skipped span; Race/Item/Play returning None is normal and silent.
            hint_url: str | None = None
            if source == AbilitySource.Class and self._pack_id is not None:
                hint_url = reference_url_for_ability(
                    pack=self._pack_id,
                    source="Class",
                    ability_name=label,
                    owning_class_name=None,  # hints have no bound class owner
                )
                # hint_url is always None here (no owner) — emit skipped span.
                with reference_url_skipped_span(
                    kind="ability",
                    pack=self._pack_id,
                    world=None,
                    keys=("?", label),
                    reason="no_owner_in_scope",
                ):
                    pass

            abilities.append(
                AbilityDefinition(
                    name=label,
                    genre_description=description,
                    mechanical_effect=hint_key,
                    involuntary=False,
                    source=source,
                    reference_url=hint_url,
                )
            )
        span.add_event(
            "chargen.abilities_resolved",
            {
                "count": len(abilities),
                "names": ", ".join(a.name for a in abilities),
            },
        )

        # World-tier origin trait (89-5): a chargen choice may grant a
        # dual-voice Race-source ability (the Barsoom Earthman gravity boon).
        # The trait DEFINITION lives in the world's char_creation.yaml choice
        # — never keyed off the race string in engine code — so non-barsoom
        # builds with race "Earthman" correctly receive nothing. The stat
        # half of such a boon rides the same choice's stat_bonuses (already
        # consumed additively by generate_stats); this seam wires the
        # ability half and the lie-detector event.
        if acc.origin_trait is not None:
            trait = acc.origin_trait
            abilities.append(
                AbilityDefinition(
                    name=trait.name,
                    genre_description=trait.genre_description,
                    mechanical_effect=trait.mechanical_effect,
                    involuntary=trait.involuntary,
                    source=AbilitySource.Race,
                    reference_url=None,
                )
            )
            from sidequest.telemetry.spans import SPAN_CHARGEN_ORIGIN_TRAIT_APPLIED

            span.add_event(
                SPAN_CHARGEN_ORIGIN_TRAIT_APPLIED,
                {
                    "origin": race_str,
                    "ability_names": trait.name,
                    "stat_bonuses": str(dict(acc.stat_bonuses)),
                },
            )

        # Class signature seeding (spec 2026-05-10 §6.1).
        # Resolve the ClassDef from the pack's class list using class_str.
        # No-op when self._classes is empty (packs without classes.yaml)
        # or when the class name isn't found (silently skips rather than
        # raising — a missing class will already have been flagged by the
        # class_kit_unresolved event above or by a future gate).
        _resolved_class_def = next((c for c in self._classes if c.display_name == class_str), None)
        if _resolved_class_def is not None:
            _class_seed_start = len(abilities)
            _seed_class_abilities(abilities, _resolved_class_def, pack_id=self._pack_id)
            _class_seed_count = len(abilities) - _class_seed_start
            from sidequest.telemetry.spans import SPAN_CHARGEN_CLASS_ABILITIES_SEEDED

            span.add_event(
                SPAN_CHARGEN_CLASS_ABILITIES_SEEDED,
                {
                    "class_id": _resolved_class_def.id,
                    "ability_count": _class_seed_count,
                    "ability_names": ", ".join(a.name for a in abilities[_class_seed_start:]),
                },
            )

        # Stub seam — next story owns Item-source ability population (spec §6.2).
        _seed_item_abilities(abilities, kit_def=getattr(self, "_kit_def", None))

        # HpPool seeding (ADR-114): re-points the ADR-078 edge_config seed
        # from Edge to ablative HP. The genre-model field is still named
        # `edge_config`; we read its `base_max_by_class` for the HP pool
        # (the genre-model field is not renamed in this task). Missing
        # class → raise the builder's HpConfigMissingClassError, not the
        # core module's error directly. Rolled CON feeds the seed so every
        # class gets a CON modifier (class_base + con_mod).
        if self._edge_config is not None:
            con_score = int(stats.get("CON", 10))
            con_modifier = (con_score - 10) // 2
            try:
                hp = hp_pool_from_config(self._edge_config, class_str, con_score=con_score)
            except _CoreHpConfigMissingClassError as e:
                raise HpConfigMissingClassError(class_name=e.class_name) from None
            span.add_event(
                "chargen.hp_seeded",
                {
                    "source": "edge_config",
                    "class": class_str,
                    "base_max": hp.base_max,
                    "con_score": con_score,
                    "con_modifier": con_modifier,
                    "seed_formula": "class_base+con_mod",
                },
            )
        else:
            hp = HpPool(current=10, max=10, base_max=10)
            span.add_event(
                "chargen.hp_seeded",
                {
                    "source": "default",
                    "class": class_str,
                    "base_max": hp.base_max,
                    "reason": "genre pack has no hp config",
                    "severity": "warn",
                },
            )

        # SystemStrainPool seeding (CWN): max == Body/CON-flavor score.
        # Non-cwn packs get None; seed_system_strain is a module-level
        # pure helper (unit-testable without constructing a full builder).
        system_strain = seed_system_strain(self._rules, stats)

        # WWN Effort pools + spellcasting state (wwn packs, magic classes).
        # Non-wwn / non-magic classes get ({}, None) — no silent partial state.
        # _resolved_class_def is the ClassDef resolved from class_str above.
        wwn_effort, wwn_spellcasting = seed_wwn_magic(self._rules, stats, _resolved_class_def)

        # Resolved archetype: pairs jungian_hint / rpg_role_hint if both
        # are present. archetype_provenance is populated downstream by
        # dispatch (connect.rs) once the tiered resolver runs.
        resolved_archetype = None
        if acc.jungian_hint is not None and acc.rpg_role_hint is not None:
            resolved_archetype = f"{acc.jungian_hint}/{acc.rpg_role_hint}"

        # Canned-openings P2: split the lobby/chargen name into first/last
        # parts and stamp the chosen background/drive choice LABELS onto
        # the Character. Defaults to "" when a genre's chargen flow has
        # no background- or drive-shaped scene — the helper that consumes
        # these fields treats "" as explicit absence (not a silent
        # fallback).
        first_name, last_name = _split_name(name)

        # Story 93-2: durable provenance of the player's chargen answers —
        # one entry per ANSWERED scene in scene-walk order. Auto-advance
        # acks and the arrangement confirm append a SceneResult but carry
        # no prompt/answer pair (choice_label is None), so they are
        # skipped. Results align with scenes by index (every applied scene
        # appends exactly one result — the same invariant the abilities
        # loop above relies on); freeform/story results also carry an
        # explicit scene_id stamp which wins when present.
        creation_answers: list[CreationAnswer] = []
        # strict=False: results ≤ scenes always (build runs from the
        # Confirmation phase, where every walked scene appended exactly
        # one result; barsoom-style packs with a trailing display scene
        # can legitimately have fewer results than scenes after revert).
        for scene_for_answer, result in zip(self._scenes, self._results, strict=False):
            answer_scene_id = result.scene_id or scene_for_answer.id
            if isinstance(result.input_type, FreeformInput):
                creation_answers.append(
                    CreationAnswer(
                        scene_id=answer_scene_id,
                        prompt=scene_for_answer.title,
                        kind="freeform",
                        value=result.input_type.text,
                    )
                )
            elif isinstance(result.input_type, StoryInput):
                # the_story folds background + description; pronouns are
                # mechanical (pronoun_hint), not narrative words. Same join
                # _apply_story uses for MechanicalEffects.background.
                story_parts = [
                    result.input_type.background.strip(),
                    result.input_type.description.strip(),
                ]
                creation_answers.append(
                    CreationAnswer(
                        scene_id=answer_scene_id,
                        prompt=scene_for_answer.title,
                        kind="freeform",
                        value=" | ".join(p for p in story_parts if p),
                    )
                )
            elif isinstance(result.input_type, ChoiceInput) and result.choice_label is not None:
                creation_answers.append(
                    CreationAnswer(
                        scene_id=answer_scene_id,
                        prompt=scene_for_answer.title,
                        kind="choice",
                        value=result.choice_label,
                    )
                )
        span.add_event(
            "chargen.creation_answers_recorded",
            {
                "count": len(creation_answers),
                "choice_count": sum(1 for a in creation_answers if a.kind == "choice"),
                "freeform_count": sum(1 for a in creation_answers if a.kind == "freeform"),
                "scene_ids": ", ".join(a.scene_id for a in creation_answers),
            },
        )

        # Compose the Character. Character / CreatureCore non-blank
        # validators will catch blank name / description / personality.
        character = Character(
            core=CreatureCore(
                name=name,
                description=(f"{indefinite_article(race_str).capitalize()} {race_str} {class_str}"),
                personality=acc.personality_trait or "Determined",
                level=1,
                xp=0,
                inventory=Inventory(items=items, gold=0),
                statuses=[],
                hp=hp,
                system_strain=system_strain,
                effort=wwn_effort,
                spellcasting=wwn_spellcasting,
                acquired_advancements=[],
            ),
            backstory=backstory_text,
            narrative_state="Beginning their adventure",
            hooks=hooks,
            char_class=class_str,
            race=race_str,
            pronouns=acc.pronoun_hint or "",
            stats=stats,
            abilities=abilities,
            known_facts=[],
            affinities=[],
            is_friendly=True,
            resolved_archetype=resolved_archetype,
            archetype_provenance=None,
            background=acc.background_label or "",
            drive=acc.backstory_label or "",
            # Display-only flavor labels (symmetric with background_label).
            # acc.race_label / acc.class_label capture a CHOICE's chosen flavor
            # when it differs from the collapsed mechanical hint; empty when the
            # label IS the archetype. The live sheet shows these over the slug.
            origin_label=acc.race_label or "",
            # A genuine vocation-flavor label wins (tea_and_murder "Country
            # Veterinary Surgeon"); otherwise fall back to the mechanical
            # class_hint ("Channeler") so combined-origin packs get a real
            # Discipline identity instead of an empty calling. class_hint must
            # win over the origin display label — never duplicate origin_label.
            calling_label=acc.class_label or acc.class_hint or "",
            first_name=first_name,
            last_name=last_name,
            nickname="",
            creation_answers=creation_answers,
        )

        return character

    # --- Scene filtering ---

    def _filter_class_choices(self, scene: CharCreationScene) -> CharCreationScene:
        """If this scene's choices encode class_hint values AND we have a
        loaded class list, drop choices whose class doesn't qualify against
        current rolled stats."""
        if not self._classes or not scene.choices:
            return scene
        if not all(c.mechanical_effects.class_hint for c in scene.choices):
            return scene
        if self._rolled_stats is None:
            return scene
        stats_dict = dict(self._rolled_stats)
        qualifying_names = {c.display_name for c in qualifying_classes(stats_dict, self._classes)}
        kept = [c for c in scene.choices if c.mechanical_effects.class_hint in qualifying_names]
        return scene.model_copy(update={"choices": kept})

    # --- Stat generation ---

    def _roll_3d6_arrange_visible(self) -> None:
        """Roll six 3d6 totals into an unlabeled pool.

        No qualification loop. The arrangement scene resolves which stat
        gets which roll, and rejection is the only escape valve. Stat
        labels come from ``self._ability_score_names``.
        """
        self._arrangement_pool = [
            self._rng.randint(1, 6) + self._rng.randint(1, 6) + self._rng.randint(1, 6)
            for _ in range(6)
        ]
        self._arrangement_assignment = {name: None for name in self._ability_score_names}
        # rolled_stats stays None until confirm_arrangement materializes it.

    def _roll_3d6_strict(self) -> None:
        """Roll 3d6 stats once into self._rolled_stats.

        No qualification loop. C&C uses the visible
        ``roll_3d6_arrange_visible`` flow with a player-driven reject
        button instead. This method remains for genres whose chargen
        flow is in-order roll-and-stop with no qualification gate.
        """
        self._rolled_stats = self._roll_3d6_stats()
        if self._rolled_stats is not None and self._classes:
            qual = qualifying_classes(dict(self._rolled_stats), self._classes)
            trace.get_current_span().add_event(
                "chargen.class_qualifying",
                {"class_ids": [c.id for c in qual]},
            )

    def _roll_3d6_stats(self) -> list[tuple[str, int]]:
        """Roll 3d6 for each ability score in order. Returns ``(name, total)``
        pairs in ``ability_score_names`` order.

        Uses the builder's seedable RNG so tests can drive deterministic
        outputs.
        """
        from sidequest.telemetry.spans import SPAN_CHARGEN_STAT_ROLL, Emitter

        rng = self._rng
        results: list[tuple[str, int]] = []
        for name in self._ability_score_names:
            dice = (rng.randint(1, 6), rng.randint(1, 6), rng.randint(1, 6))
            total = sum(dice)
            Emitter.fire(
                SPAN_CHARGEN_STAT_ROLL,
                {
                    "stat": name,
                    "dice": list(dice),
                    "total": total,
                },
            )
            results.append((name, total))
        return results

    @staticmethod
    def _allocate_point_buy(n: int, budget: int) -> list[int]:
        """Allocate a point-buy budget across `n` stats.

        All stats start at 8. Points distributed round-robin, raising each
        stat by 1 at a time (cheapest-first) until budget is spent. No
        stat can exceed 15. Cost table (cumulative from 8):
          8→9..12: 1pt each; 13→14..15: 2pt each.
        """

        def marginal_cost(value: int) -> int:
            if 9 <= value <= 13:
                return 1
            if value in (14, 15):
                return 2
            # Outside [9, 15] — effectively infinite; callers filter via
            # the next_val > 15 guard before reaching this branch.
            return 1 << 30

        stats = [8] * n
        remaining = budget
        while True:
            any_raised = False
            for i in range(n):
                next_val = stats[i] + 1
                if next_val > 15:
                    continue
                cost = marginal_cost(next_val)
                if cost <= remaining:
                    stats[i] = next_val
                    remaining -= cost
                    any_raised = True
            if not any_raised or remaining == 0:
                break
        return stats

    def generate_stats(self, acc: AccumulatedChoices) -> dict[str, int]:
        """Generate ability scores per the declared stat_generation method.

        Strategies:
        - roll_3d6_strict: reuse pre-rolled stats from construction or
          scene directive; re-roll inline if absent (defensive — the
          eager roll should have fired).
        - standard_array: [15, 14, 13, 12, 10, 8] mapped to the
          ability_score_names in declaration order. When no explicit
          stat_bonuses were set by chargen choices, derive bonuses from
          accumulated hints (race/mutation/class) to differentiate stat
          spreads across builds.
        - point_buy: distribute point_buy_budget across ability scores.

        Accumulated `acc.stat_bonuses` are applied additively on top of
        the generated baseline (every strategy).

        Raises UnknownStatGenerationError for any other method string.
        """
        method = self._stat_generation

        if method == "roll_3d6_strict":
            if self._rolled_stats is not None:
                stats = dict(self._rolled_stats)
            else:
                # Defensive re-roll — shouldn't fire in practice because
                # the eager construction roll covers this path.
                rolled = self._roll_3d6_stats()
                stats = dict(rolled)

        elif method == "standard_array":
            base_values = [15, 14, 13, 12, 10, 8]
            stats = dict(zip(self._ability_score_names, base_values, strict=False))

        elif method == "point_buy":
            values = self._allocate_point_buy(
                len(self._ability_score_names), self._point_buy_budget
            )
            stats = dict(zip(self._ability_score_names, values, strict=True))

        else:
            raise UnknownStatGenerationError(method=method)

        # Apply explicit stat bonuses from chargen choices (origin,
        # mutation, artifact).
        for stat, bonus in acc.stat_bonuses.items():
            if stat in stats:
                stats[stat] += bonus

        # Standard-array derivation: when no explicit bonuses were
        # authored and we have at least 3 stats, differentiate the
        # spread using accumulated hints.
        if (
            not acc.stat_bonuses
            and method == "standard_array"
            and len(self._ability_score_names) >= 3
        ):
            names = self._ability_score_names
            # Origin/race → boost first stat
            if acc.race_hint is not None:
                stats[names[0]] = stats[names[0]] + 3
            # Mutation/affinity → boost second stat, reduce last
            if acc.mutation_hint is not None or acc.affinity_hint is not None:
                stats[names[1]] = stats[names[1]] + 2
                stats[names[-1]] = stats[names[-1]] - 1
            # Class/training → boost third stat (floor at last index if
            # fewer than 3 names, though the guard above already rejects
            # that case).
            if acc.class_hint is not None or acc.training_hint is not None:
                idx = min(2, len(names) - 1)
                stats[names[idx]] = stats[names[idx]] + 2

        import json as _json

        from sidequest.telemetry.spans import SPAN_CHARGEN_STATS_GENERATED, Emitter

        Emitter.fire(
            SPAN_CHARGEN_STATS_GENERATED,
            {
                "method": method,
                "stat_count": len(stats),
                "stats_json": _json.dumps(dict(stats), sort_keys=True),
            },
        )
        return stats

    # --- Private helpers ---

    def _advance_scene(self, current: int) -> None:
        """Advance to the next scene, or transition to Confirmation if
        `current` was the last scene."""
        next_index = current + 1
        if next_index >= len(self._scenes):
            self._phase = CONFIRMATION
        else:
            self._phase = InProgress(scene_index=next_index)

    def _phase_name(self) -> str:
        """Human-readable phase name for error messages."""
        match self._phase:
            case InProgress():
                return "InProgress"
            case AwaitingFollowup():
                return "AwaitingFollowup"
            case Confirmation():
                return "Confirmation"
            case _:  # pragma: no cover
                return "Unknown"


__all__ = [
    # Hooks and anchors
    "HookType",
    "NarrativeHook",
    "LoreAnchor",
    "extract_hooks",
    "extract_anchors",
    # Scene input variants
    "SceneInputType",
    "ChoiceInput",
    "FreeformInput",
    "StoryInput",
    # Scene result and accumulation
    "SceneResult",
    "AccumulatedChoices",
    # Phase state machine
    "BuilderPhase",
    "InProgress",
    "AwaitingFollowup",
    "Confirmation",
    "CONFIRMATION",
    # Errors
    "BuilderError",
    "InvalidChoiceError",
    "WrongPhaseError",
    "FreeformNotAllowedError",
    "NoScenesError",
    "CannotRevertError",
    "UnknownStatGenerationError",
    "NumericNameError",
    "HpConfigMissingClassError",
    "PoolValueNotPresentError",
    "UnfilledArrangementError",
    "NoQualifyingClassesError",
    "ArrangementSceneActiveError",
    # Builder
    "CharacterBuilder",
    # String helpers
    "humanize_snake_case",
    "strip_unmatched_placeholders",
    "find_unrecognized_tokens",
]
