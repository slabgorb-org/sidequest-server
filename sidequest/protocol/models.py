"""Nested model types shared across Phase 1 protocol payloads.

Port of the sub-types defined in sidequest-protocol/src/message.rs that are
referenced transitively by the Phase 1 GameMessage payloads. All types live in
this single file — do not fragment into sub-modules.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from sidequest.protocol.base import ProtocolBase
from sidequest.protocol.dice import ThrowParams
from sidequest.protocol.provenance import Provenance
from sidequest.protocol.types import NonBlankString

# ---------------------------------------------------------------------------
# AbilitySource / AbilityDefinition
# ---------------------------------------------------------------------------
# Defined here (not in sidequest.game.ability) to avoid triggering the
# sidequest.game package __init__ during protocol import, which creates a
# circular dependency through genre/archetype/resolved → protocol → game →
# genre → game. sidequest.game.ability re-exports AbilitySource from here.
# ---------------------------------------------------------------------------


class AbilitySource(StrEnum):
    """How a character acquired an ability."""

    Race = "Race"
    """Innate to the character's race/species."""
    Class = "Class"
    """Granted by the character's class/archetype."""
    Item = "Item"
    """Bestowed by an item or artifact."""
    Play = "Play"
    """Acquired during gameplay through experience."""


class AbilityDefinition(BaseModel):
    """Dual-voice ability representation.

    genre_description: player-facing narrative description.
    mechanical_effect: engine-facing trigger text.
    involuntary: if True, narrator can trigger without player choice.
    source: how the character acquired this ability (Race/Class/Item/Play).
    reference_url: optional hyperlink into /reference/rules/<pack> for
        Class-source signature abilities.  Populated server-side when
        source == Class and the ability resolves to a known classes.yaml
        signature; None for Race/Item/Play sources or when the binding
        cannot be resolved.
    """

    model_config = {"extra": "forbid"}

    name: str
    genre_description: str
    mechanical_effect: str
    involuntary: bool = False
    source: AbilitySource
    reference_url: str | None = None
    """URL into /reference/rules/<pack> for the class signature that grants
    this ability.  Populated server-side when source == Class and the
    ability resolves to a known classes.yaml signature; None for
    Race/Item/Play sources or when the binding cannot be resolved."""


# ---------------------------------------------------------------------------
# FactCategory — from Footnote / Journal
# ---------------------------------------------------------------------------


class FactCategory(StrEnum):
    """Classification category for narrator footnotes.

    Port of sidequest_protocol::FactCategory.
    """

    Lore = "Lore"
    """World history, mythology, or cosmology."""
    Place = "Place"
    """Geographic location or landmark."""
    Person = "Person"
    """NPC, faction, or named individual."""
    Quest = "Quest"
    """Quest objective, task, or mission."""
    Ability = "Ability"
    """Character ability, skill, or power."""


# ---------------------------------------------------------------------------
# Footnote — narration knowledge extraction
# ---------------------------------------------------------------------------


class Footnote(ProtocolBase):
    """A structured footnote from narrator output.

    Port of sidequest_protocol::Footnote.
    """

    marker: int | None = None
    """Marker number matching [N] superscript in prose. Optional."""
    fact_id: str | None = None
    """Links to existing KnownFact if this is a callback (is_new: false)."""
    summary: NonBlankString
    """One-sentence description of the fact. Non-blank."""
    category: FactCategory
    """Classification category for the footnote."""
    is_new: bool
    """True if this is a new revelation, false if referencing prior knowledge."""


# ---------------------------------------------------------------------------
# JournalEntry — JOURNAL_RESPONSE row (ADR-100 Seam C, story 50-14)
# ---------------------------------------------------------------------------


class JournalEntry(ProtocolBase):
    """A single character-journal entry for the JOURNAL_RESPONSE payload.

    Mirrors the UI's journal-row contract (see
    ``sidequest-ui/src/types/payloads.ts:248``). Derived 1:1 from a
    :class:`~sidequest.game.character.KnownFact`.
    """

    fact_id: str
    """Stable identifier — UI dedups by this across multiple responses."""
    content: str
    """Fact text."""
    category: FactCategory
    """Lore / Place / Person / Quest / Ability."""
    source: str
    """Provenance label (Observation, ScenarioClue, Gossip, GameEvent, ...)."""
    confidence: str
    """confirmed / suspected / rumored / Discovered / ..."""
    learned_turn: int
    """Interaction-turn index at the moment the fact was learned."""
    reference_url: str | None = None
    """URL into the lore page for legend / history / location entries.
    None for Person (npcs excluded), Quest (no rendered yaml), Ability
    (handled via AbilityDefinition.reference_url), and Lore/Place
    entries whose content text doesn't match a known YAML entity."""


# ---------------------------------------------------------------------------
# ItemGained — inventory addition during narration
# ---------------------------------------------------------------------------


class ItemGained(ProtocolBase):
    """An item the player gained during narration.

    Port of sidequest_protocol::ItemGained.
    """

    name: NonBlankString
    """Short item name. Non-blank."""
    description: NonBlankString = Field(
        default_factory=lambda: NonBlankString.model_validate("An item found during adventure.")
    )
    """One-sentence description. Non-blank."""
    category: str = "misc"
    """Category (weapon, armor, tool, consumable, quest, misc)."""


# ---------------------------------------------------------------------------
# CharacterState — character snapshot in state deltas
# ---------------------------------------------------------------------------


class CharacterState(ProtocolBase):
    """Character state as seen by the client (UI-facing).

    Port of sidequest_protocol::CharacterState.
    """

    name: NonBlankString
    """Character name (merge key). Non-blank."""
    hp: int
    """Current hit points."""
    max_hp: int
    """Maximum hit points."""
    level: int = 0
    """Character level."""
    class_: str = Field("", alias="class")
    """Character class (e.g., 'Ranger', 'Mage')."""
    statuses: list[str]
    """Active status effects."""
    inventory: list[str]
    """Inventory item names."""
    archetype_provenance: Provenance | None = None
    """Provenance of the resolved archetype, when available."""


# ---------------------------------------------------------------------------
# StateDelta — state mutations carried in NARRATION and TURN_STATUS
# ---------------------------------------------------------------------------


class PartyFormationWireEntry(ProtocolBase):
    """Wire-side party formation entry — story 45-1 sealed-letter handshake.

    Mirrors :class:`sidequest.game.shared_world_delta.PartyFormationEntry`
    but lives on the protocol boundary so non-Python clients can decode it
    without pulling game-side types. Carries canonical placement only —
    perceived state (mood/tactics/personality) never lands here.
    """

    player_id: str
    """Player_id whose character occupies this slot."""
    location: str
    """Canonical room/POI for this PC."""
    adjacency: list[str]
    """Other player_ids sharing this location."""


class StateDelta(ProtocolBase):
    """State changes carried in NARRATION and TURN_STATUS.

    Port of sidequest_protocol::StateDelta.
    All fields are optional — only changed state is included.

    Story 45-1 added ``encounter_id`` and ``party_formation`` so the
    sealed-letter shared-world handshake can ride NARRATION_END alongside
    the existing location field.
    """

    location: str | None = None
    """New location, if changed."""
    characters: list[CharacterState] | None = None
    """Updated character states, merged by name."""
    quests: dict[str, str] | None = None
    """Updated quest statuses, merged by key."""
    items_gained: list[ItemGained] | None = None
    """Items gained by the player this turn."""
    encounter_id: str | None = None
    """Active encounter id (encounter_type), or None when no encounter is live."""
    party_formation: list[PartyFormationWireEntry] | None = None
    """Per-player canonical placement — story 45-1 sealed-letter handshake."""
    magic_state: dict | None = None
    """Opaque magic-state payload when MagicState changed this turn (Task 2.4).
    None when magic is inactive or unchanged. Client deserializes via TS types."""


# ---------------------------------------------------------------------------
# InitialState — session boot state
# ---------------------------------------------------------------------------


class InitialState(ProtocolBase):
    """Initial game state sent on session ready.

    Port of sidequest_protocol::InitialState.
    """

    characters: list[CharacterState]
    """Party characters."""
    location: NonBlankString
    """Current location. Non-blank."""
    quests: dict[str, str]
    """Quest log."""
    turn_count: int = 0
    """Current turn count (persisted across sessions)."""


# ---------------------------------------------------------------------------
# CreationChoice — chargen option
# ---------------------------------------------------------------------------


class CreationChoice(ProtocolBase):
    """A choice in the character creation flow.

    Port of sidequest_protocol::CreationChoice.
    """

    label: NonBlankString
    """Display label. Non-blank — rendered as the button text."""
    description: NonBlankString
    """Description text. Non-blank — rendered below the label."""


# ---------------------------------------------------------------------------
# RolledStat — chargen rolled ability score
# ---------------------------------------------------------------------------


class RolledStat(ProtocolBase):
    """One rolled ability score: ability name + value.

    Port of sidequest_protocol::RolledStat.
    """

    name: str
    """Ability name as defined by the genre's ability_score_names."""
    value: int
    """Rolled value (typically 3-18 for 3d6 strict)."""


# ---------------------------------------------------------------------------
# ClassRequirement — chargen the_arrangement live-qualify panel row
# ---------------------------------------------------------------------------


class ClassRequirement(ProtocolBase):
    """One row in the live-qualify panel during the_arrangement scene."""

    name: str
    """Class display name (e.g. 'Fighter')."""
    requirement_label: str
    """Human-readable requirement (e.g. 'STR 9+')."""


# ---------------------------------------------------------------------------
# InventoryItem — item entry in inventory
# ---------------------------------------------------------------------------


class InventoryItem(ProtocolBase):
    """An inventory item.

    Port of sidequest_protocol::InventoryItem.
    """

    name: NonBlankString
    """Item name. Non-blank — rendered as the inventory row header."""
    item_type: str = Field(alias="type")
    """Item category (weapon, armor, consumable, etc.)."""
    equipped: bool
    """Whether the item is equipped."""
    quantity: int
    """Stack count."""
    description: NonBlankString
    """Item description. Non-blank — rendered below the name."""


# ---------------------------------------------------------------------------
# InventoryPayload — full inventory snapshot
# ---------------------------------------------------------------------------


class InventoryPayload(ProtocolBase):
    """Full inventory snapshot.

    Port of sidequest_protocol::InventoryPayload.
    """

    items: list[InventoryItem]
    """All inventory items."""
    gold: int
    """Currency amount. Numeric — the noun is ``currency_name`` below."""
    currency_name: str | None = None
    """Genre-declared currency noun (e.g. "credits" for space_opera,
    "Salvage" for mutant_wasteland, "gold" for caverns_and_claudes). Read
    from ``inventory.yaml::currency.name`` on the active genre pack.
    ``None`` when the genre pack doesn't declare one — UI falls back to
    a neutral default rather than hardcoding "gold" (which leaks fantasy
    tone into space/cyberpunk/etc. packs)."""
    wealth_tier_label: str | None = None
    """Player-facing wealth tier (ADR-021 track 3) — the ``gold`` balance
    resolved against the pack's ``progression.wealth_tiers`` (e.g. "stocked",
    "convoy legend"). ``None`` when the pack authors no wealth tiers; the UI
    then shows the bare number. Mechanics-first legibility: wealth reads as a
    tier, not just a count."""


# ---------------------------------------------------------------------------
# ClassMove — a resolved encounter-beat choice for the Abilities panel
# ---------------------------------------------------------------------------


class ClassMove(ProtocolBase):
    """A player-facing class move (confrontation beat) for the Abilities panel.

    The class's ``encounter_beat_choices`` are bare beat IDs; this resolves
    each to its authored ``label`` + a player-readable ``description`` so the
    UI renders "Cross-Examine" with a tooltip instead of the raw token
    ``cross_examine`` (playtest 2026-05-21 tea_and_murder/glenross UX bug).
    """

    id: str
    """Stable beat id (e.g. ``cross_examine``) — the confrontation engine key."""
    label: str
    """Display label from the BeatDef (e.g. "Cross-Examine")."""
    description: str | None = None
    """Player-readable hint: BeatDef flavor → narrator_hint → effect, whichever
    is present first. ``None`` when the beat carries no descriptive text."""


# ---------------------------------------------------------------------------
# CreationAnswer — durable chargen provenance (Story 93-2)
# ---------------------------------------------------------------------------
# Defined here (not in sidequest.game.character) for the same layering reason
# as AbilityDefinition above: the Character model and the protocol sheet both
# carry it, and game already depends on protocol. sidequest.game.character
# re-exports it.
# ---------------------------------------------------------------------------


class CreationAnswer(BaseModel):
    """One answered chargen scene — the player's words or their pick.

    Story 93-2: the per-scene answer the player actually gave, recorded
    durably on the Character (and carried in the snapshot sheet) instead of
    being consumed for prose slots and discarded. Only ANSWERED scenes are
    recorded — auto-advance acks and the arrangement confirm carry no
    prompt/answer pair.
    """

    model_config = {"extra": "forbid"}

    scene_id: str
    """The chargen scene that asked the question."""
    prompt: str
    """The scene's question/title, as the player saw it."""
    kind: Literal["choice", "freeform"]
    """How the player answered: a canned pick or their own words."""
    value: str
    """The chosen option LABEL (choice) or the player's verbatim text
    (freeform) — never a derived/collapsed mechanical hint."""
    archetype_inferred: bool = False
    """True iff this answer's text fed the 93-1 Haiku archetype inference —
    the UI badges these ('inferred from your words'). Marked at the chargen
    confirm seam, never by builder.build() itself."""


class LinkedLoreFragment(BaseModel):
    """One creation-seed lore fragment linked to a character's History.

    Story 93-4: a typed, sheet-ready projection of an ADR-048 lore fragment
    that belongs to THIS character (its chosen chargen options), surfaced as
    a 'Lore' subsection beneath the 93-3 origin block. Plumbing only — the
    fragments are authored by chargen seeding (story 75-15 /
    :func:`seed_lore_from_char_creation`), never minted here.
    """

    model_config = {"extra": "forbid"}

    fragment_id: str
    """The ADR-048 store id (``lore_char_creation_<scene_id>_<choice_index>``)."""
    title: str
    """Display heading — the chosen option label."""
    summary: str
    """The fragment body (``"<label>: <description>"``)."""
    source: str
    """The fragment's LoreSource tag (``character_creation``)."""
    lore_route: str | None = None
    """Link to the fragment's lore page when one exists, else None. The UI
    renders the title as a link only when this is set — never a fabricated
    href (No Silent Fallbacks)."""


# ---------------------------------------------------------------------------
# CharacterSheetDetails — full character sheet nested inside PartyMember
# ---------------------------------------------------------------------------


class CharacterSheetDetails(ProtocolBase):
    """Character sheet details nested inside PartyMember.

    Port of sidequest_protocol::CharacterSheetDetails.
    """

    race: NonBlankString
    """Character race/origin. Non-blank post-chargen."""
    origin_label: NonBlankString | None = None
    """Display-only flavor label for Origin (the chosen chargen phrase, e.g.
    "The Village Itself"). None when the label IS the mechanical archetype —
    the UI falls back to ``race``. Diamonds-and-Coal: flavor on the sheet."""
    calling_label: NonBlankString | None = None
    """Display-only flavor label for Calling (e.g. "Country Veterinary
    Surgeon"). None when the label IS the mechanical class — the UI falls
    back to the top-level ``class``."""
    stats: dict[str, int]
    """Ability scores / stats."""
    abilities: list[AbilityDefinition]
    """Full ability records, including source classification."""
    class_moves: list[ClassMove] = Field(default_factory=list)
    """Pre-filtered encounter_beat_choices (universal beats + scaffolding
    stripped), each resolved to its label + player-readable description."""
    backstory: NonBlankString
    """Character backstory. Non-blank post-chargen."""
    personality: NonBlankString
    """Personality trait. Non-blank post-chargen."""
    pronouns: NonBlankString | None = None
    """Pronouns. Optional. Non-blank when present."""
    equipment: list[str] = Field(default_factory=list)
    """Equipped/carried items as display strings."""
    creation_answers: list[CreationAnswer] = Field(default_factory=list)
    """Chargen provenance (Story 93-2): the player's per-scene answers in
    scene-walk order — rendered by the 93-3 History section. Empty for
    pre-93-2 characters."""
    lore_fragments: list[LinkedLoreFragment] = Field(default_factory=list)
    """Player-linked creation-seed lore fragments (Story 93-4) — rendered as
    a 'Lore' subsection beneath the 93-3 origin block. Filtered to THIS
    character's chosen chargen options; another player's picks never leak in.
    Empty when the character has no linked fragments (legacy / no-store)."""
    skills: dict[str, int] = Field(default_factory=dict)
    """WN-family skill name → level mapping (ADR-143 Task 11). Empty for
    non-WN characters and pre-ADR-143 saves. Rendered by the UI as a Skills
    section only when non-empty (mechanics-first — Sebastien/Jade legibility)."""
    foci: list[str] = Field(default_factory=list)
    """WN-family focus ids (ADR-143 Task 11). Empty for non-WN characters.
    Rendered by the UI as a Foci section only when non-empty."""
    appearance: str = ""
    """Player-authored physical appearance from chargen (Story 126-5). Empty
    for characters built without an appearance input; the UI renders the
    Appearance section only when non-empty."""
    fate_aspects: list[FateAspectEntry] = Field(default_factory=list)
    """Named Fate character aspects (high_concept / trouble / character) for the
    player sheet (Deliverable B1). Reuses the FateAspectEntry wire type. Empty
    for non-Fate characters (fate_sheet is None); the UI renders an Aspects
    section only when non-empty."""


# ---------------------------------------------------------------------------
# PartyMember — character party snapshot
# ---------------------------------------------------------------------------


class AdvancementDelta(ProtocolBase):
    """A player-facing level-up delta (ADR-021 track 1).

    Surfaced so the player *sees the advancement and its driver* — not a
    silent stat bump. Distinct from the ``progression.level_up`` OTEL/watcher
    event, which is the dev/GM lie-detector (CLAUDE.md OTEL Observability
    Principle); this is the player-UI channel (mechanics-first — Sebastien /
    Jade want the math legible).
    """

    character_name: str
    """Character that advanced."""
    before: int
    """Level before the crossing."""
    after: int
    """Level after the crossing."""
    driver: str
    """What drove the advancement (e.g. 'milestone')."""


class AffinityTierUp(ProtocolBase):
    """A player-facing affinity tier-promotion delta (ADR-021 track 2).

    Surfaced so the player *sees which affinity advanced and to what tier* — not
    a silent tier bump. The sibling of :class:`AdvancementDelta` (track 1
    level-up); it additionally carries ``affinity_id`` because a character has
    many affinities and several can advance in one turn. Distinct from the
    ``progression.affinity_tier_up`` OTEL/watcher event, which is the dev/GM
    lie-detector; this is the player-UI channel (mechanics-first — Sebastien /
    Jade want the math legible).
    """

    character_name: str
    """Character whose affinity advanced."""
    affinity_id: str
    """Which affinity advanced (matches ``Affinity.name``)."""
    before: int
    """Tier before the crossing."""
    after: int
    """Tier after the crossing."""
    driver: str
    """What drove the advancement (e.g. 'affinity')."""


class PartyMember(ProtocolBase):
    """A party member in PARTY_STATUS.

    Port of sidequest_protocol::PartyMember.
    """

    player_id: NonBlankString
    """Player identifier. Non-blank — identity key."""
    name: NonBlankString
    """Player lobby name. Non-blank."""
    player_identity: str | None = None
    """Resolved player identity (Cf-Access email / dev Host). None when the player
    is not currently connected (room-only store). Story 67-6 / ADR-119."""
    character_name: NonBlankString | None = None
    """In-game character name. Optional (None = still in chargen)."""
    current_hp: int
    """Current HP."""
    max_hp: int
    """Maximum HP."""
    survivability_pool_label: str | None = None
    """Story 68-1: per-genre label for the survivability pool (Composure /
    Standing / Poise on social packs). None ⇒ the UI renders the default
    "HP". Genre-level (uniform across the table); it rides the per-member
    frame because it labels current_hp/max_hp directly above."""
    statuses: list[str]
    """Active statuses."""
    class_: NonBlankString = Field(alias="class")
    """Character class. Non-blank."""
    level: int
    """Character level."""
    advancement: AdvancementDelta | None = None
    """ADR-021 track 1: the most recent level-up delta (before/after/driver),
    or None on turns with no advancement. Lets the player see the level change
    and why, not a silent stat bump."""
    affinity_advancements: list[AffinityTierUp] = Field(default_factory=list)
    """ADR-021 track 2: affinity tier promotions this turn (each carries
    affinity_id/before/after/driver), or empty on turns with none. A list
    because several affinities can advance in one turn. Lets the player see the
    tier change and why, not a silent bump."""
    portrait_url: str | None = None
    """Portrait URL."""
    current_location: NonBlankString | None = None
    """Current location name. Optional."""
    sheet: CharacterSheetDetails | None = None
    """Full character sheet. None until chargen completes."""
    inventory: InventoryPayload | None = None
    """Full inventory snapshot. None until the member has a loadout."""
    class_reference_url: str | None = None
    """URL to /reference/rules/<pack>#class-<slug>. Populated when the
    class is a known classes.yaml entry; None otherwise."""
    rig_composure_current: int | None = Field(
        default=None, json_schema_extra={"include_when_none": True}
    )
    """Current rig composure. None when character has no rig."""
    rig_composure_max: int | None = Field(
        default=None, json_schema_extra={"include_when_none": True}
    )
    """Maximum rig composure. None when character has no rig."""
    injury_tags: list[str] = Field(default_factory=list)
    """Crash-related injury statuses (e.g. 'injury', 'dismounted')."""
    effort_available: int | None = Field(
        default=None, json_schema_extra={"include_when_none": True}
    )
    """Story 102-6 (Sebastien/Jade legibility): free Effort the psychic can still
    commit. None when the character has no Effort pool (non-psychic) — distinct
    from 0 (a psychic with every point committed)."""
    effort_committed: int | None = Field(
        default=None, json_schema_extra={"include_when_none": True}
    )
    """Effort currently committed to active disciplines. None for non-psychics."""
    effort_max: int | None = Field(default=None, json_schema_extra={"include_when_none": True})
    """Maximum Effort pool. None for non-psychics."""
    system_strain_current: int | None = Field(
        default=None, json_schema_extra={"include_when_none": True}
    )
    """Story 102-6: accumulated System Strain (psionic pushes + lethality share
    this one counter). None when the character has no strain pool."""
    system_strain_max: int | None = Field(
        default=None, json_schema_extra={"include_when_none": True}
    )
    """Maximum System Strain. None when the character has no strain pool."""


# ---------------------------------------------------------------------------
# CompanionMember — narrator-recruited NPC companion in PARTY_STATUS
# ---------------------------------------------------------------------------


class CompanionMember(ProtocolBase):
    """A narrator-recruited NPC companion (hireling / retainer / ally).

    Surfaced in PARTY_STATUS alongside PartyMember so the Party panel
    can render the full active roster (PCs + companions). Companions
    are NOT player-controlled — they have no Edge bar / inventory /
    sheet at this tier; this payload is the minimum state the panel
    needs to display them: name, role, panel description, and the
    contract notes the narrator authored on recruit.

    Playtest 2026-05-06 wiring fix.
    """

    name: NonBlankString
    """Display name. Non-blank — identity key for dismissal lookup."""
    role: str = ""
    """Hireling role in plain prose (torchbearer, porter, scout, etc.)."""
    description: str = ""
    """One-sentence narrator-authored description for panel tooltip."""
    notes: str = ""
    """Optional contract / terms one-liner."""
    recruited_turn: int = 0
    """Interaction turn at the moment of recruitment."""
    recruited_by: str = ""
    """Acting PC's name at recruit time — \"who is this companion bonded to.\""""


# ---------------------------------------------------------------------------
# TacticalGridPayload — cellular cavern grid layout (ADR-096)
# ---------------------------------------------------------------------------


class CellularParams(ProtocolBase):
    """Cellular automata parameters for a cavern room. ADR-096."""

    size: tuple[int, int]
    """(width, height) in cells."""
    seed: int
    density: float
    cutoff: int
    passes: int


class DerivedRoomData(ProtocolBase):
    """Tool-derived room facts (exits, POIs, floor count). ADR-096."""

    floor_count: int
    exits: dict[str, tuple[int, int] | None]
    """{north|south|east|west: [x, y] | None}."""
    pois: list[tuple[int, int]]


# ---------------------------------------------------------------------------
# Location manifest (Story 54-2 / ADR-109)
# ---------------------------------------------------------------------------


class LocationEntityBinding(BaseModel):
    """Pointer to the real subsystem object backing a ``real_object`` entity.

    The cross-field invariant (``real_object`` SHOULD have a binding) is
    enforced by the ``pf validate locations`` validator (Story 54-3),
    not by pydantic. Authored content is loaded leniently; the validator
    catches mistakes at author time.
    """

    model_config = {"extra": "forbid"}

    kind: Literal["location_feature", "npc", "item", "clue", "scenario_clue"]
    ref: str = Field(min_length=1)


class LocationEntity(BaseModel):
    """A named, typed entry in a location's manifest.

    See ADR-109 §1 (three-tier manifest) and design spec §4.1
    (``docs/superpowers/specs/2026-05-19-persistent-location-descriptions-design.md``).
    The ``tier`` determines mechanical weight; ``provenance`` records how
    the entity entered the manifest.

    Authored YAML never mutates at runtime — promotions and
    player-initiated mints accumulate in the ``location_promotions``
    SQLite table (Story 54-6) and are merged on top at read time.
    """

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    tier: Literal["real_object", "yes_and", "flavor_only"]
    binding: LocationEntityBinding | None = None
    affordances: list[str] = Field(default_factory=list)
    provenance: Literal[
        "authored",
        "cookbook",
        "yes_and_promoted",
        "yes_and_minted",
    ] = "authored"
    promoted_at_turn: int | None = None
    promoted_canon: str | None = None
    reference_url: str | None = None
    """URL into /reference/lore/<pack>/<world>#location-<slug>. Populated
    server-side when pack + world + label are in scope at construction.
    The lore page's bad-anchor banner (Task 4) handles cases where the
    label doesn't match a rendered locations.yaml entry."""


class EncounterLocationOverlay(BaseModel):
    """Per-encounter contribution merged at read time. Base manifest and
    base description never mutate from overlays — see ADR-109 §5.

    Story 54-2 ships the type only. The read-time merge logic in
    ``get_location_manifest`` / ``get_location_prose`` is owned by
    Story 54-7.
    """

    model_config = {"extra": "forbid"}

    bound_room_id: str = Field(min_length=1)
    entity_delta: list[LocationEntity] = Field(default_factory=list)
    prose_suffix: str = ""


class LocationDescriptionOverlaySummary(BaseModel):
    """UI-facing summary of an active encounter overlay.

    Story 54-7 fills this with real data; 54-2 emits an empty list on
    every ``LOCATION_DESCRIPTION`` message.
    """

    model_config = {"extra": "forbid"}

    encounter_id: str
    prose_suffix: str = ""
    entity_delta_count: int = 0


class LocationDescriptionPayload(BaseModel):
    """Snapshot of one location's description + manifest for the UI.

    Emitted by ``LOCATION_DESCRIPTION``. The overlay delta channel is
    ``LOCATION_OVERLAY_CHANGED`` (Story 54-7).
    """

    model_config = {"extra": "forbid"}

    region_id: str = Field(min_length=1)
    # Authored human-readable display name for the region/room header. The
    # source is the cartography ``Region.name`` (region-mode worlds) or the
    # room YAML ``name``/``room_name`` (room-graph worlds). ``region_id`` stays
    # the snake_case key used for the lore deep-link; ``region_name`` is what
    # the player reads. None on old snapshots / sources with no authored name,
    # in which case the UI falls back to rendering ``region_id``.
    region_name: str | None = None
    prose: str
    terrain: str | None = None
    entities: list[LocationEntity] = Field(default_factory=list)
    overlays: list[LocationDescriptionOverlaySummary] = Field(default_factory=list)
    # Story 63-6: deep-link from the region header into the /reference/lore
    # wiki. None when the region has no lore-page anchor (region-mode worlds,
    # old snapshots) — the UI then renders the header as plain text.
    reference_url: str | None = None
    # POI landscape image URL for the region, built from the region_id VERBATIM
    # (the authored slug == the R2 object key, e.g. munchkin_country.png) — no
    # slugify, so it matches R2 directly. None on sources with no region. The UI
    # renders it above the prose and hides it on a load error (a region with no
    # rendered landscape 404s and degrades to text-only).
    poi_image_url: str | None = None


class LocationOverlayChangedPayload(BaseModel):
    """Delta payload for ``LOCATION_OVERLAY_CHANGED``.

    Story 54-7 / ADR-109 §5.5. Fires whenever an encounter's
    ``location_overlay`` activates or deactivates. ``overlays`` is the
    FULL current overlay set for the region after the transition (not a
    diff) so the UI can replace its overlay slice without reconciling
    enter/leave events. On activate that's one item; on deactivate it
    is an empty list.
    """

    model_config = {"extra": "forbid"}

    region_id: str = Field(min_length=1)
    overlays: list[LocationDescriptionOverlaySummary] = Field(default_factory=list)


class DispositionBeatPayload(BaseModel):
    """One disposition shift surfaced to the player (ADR-136)."""

    model_config = {"extra": "forbid"}

    turn: int
    delta: int
    reason: str
    location: str | None = None


class RelationshipClaimPayload(BaseModel):
    """A claim-to-party + coarse credibility hint (ADR-136 claims firewall)."""

    model_config = {"extra": "forbid"}

    text: str
    credibility_hint: str


class RelationshipEntry(BaseModel):
    """One NPC's player-visible relationship state (ADR-136).

    ``band`` is the 5-level display label; ``disposition`` is the raw reveal.
    ``ocean`` is an OceanProfile dump (full keys, 0..10) or None. ``personality_read``
    and ``claims`` are empty until Phases B/C.
    """

    model_config = {"extra": "forbid"}

    name: str
    portrait_url: str | None = None
    band: str
    disposition: int
    trend: str
    last_seen_turn: int
    last_seen_location: str | None = None
    beats: list[DispositionBeatPayload] = Field(default_factory=list)
    personality_read: str | None = None
    ocean: dict[str, float] | None = None
    claims: list[RelationshipClaimPayload] = Field(default_factory=list)


class RelationshipsPayload(BaseModel):
    """Full relationship roster snapshot (ADR-136)."""

    model_config = {"extra": "forbid"}

    entries: list[RelationshipEntry] = Field(default_factory=list)


class QuestLoreEntry(BaseModel):
    """One discovered lore fragment cohered under its quest (Story 117-5).

    The player-facing "what I've learned about this job" surface. Projected by
    the structural anchor→clue→fact join (ADR-053 + ADR-100 + ADR-146): a
    ScenarioClue-sourced ``KnownFact`` whose ``fact_id`` (== originating clue id
    per 50-14) belongs to a clue node touching this quest's ``anchor_id`` (via
    ``ClueNode.locations``/``implicates``). ``fact_id`` is carried for UI dedup
    against the broader KnownFacts surface; ``content`` is the readable fragment.
    """

    model_config = {"extra": "forbid"}

    fact_id: str
    content: str


class QuestLogEntry(BaseModel):
    """One quest's player-visible state (ADR-137 / Story 77-8).

    The wire projection of a stored ``QuestEntry`` (game/session.py), keyed by
    its quest id. ``anchor_id`` links to the body/location anchor where the
    objective resolves (orbital course planner consumes anchors per ADR-130).

    ``related_lore`` (Story 117-5) coheres the discovered ScenarioClue facts the
    party has learned about this quest's anchor — the "knowledge pulled into a
    coherent picture" the playgroup was missing. Empty when nothing is learned;
    never None.
    """

    model_config = {"extra": "forbid"}

    quest_id: str
    title: str = ""
    objective: str = ""
    status: str = "active"
    anchor_id: str | None = None
    related_lore: list[QuestLoreEntry] = Field(default_factory=list)


class QuestAnchorEntry(BaseModel):
    """One quest anchor's player-visible state (ADR-137 / Story 77-8).

    ``anchor_id`` is the stored body id (``GameSnapshot.quest_anchors``).
    ``quest_id`` is the quest that owns this anchor (matched via
    ``QuestEntry.anchor_id``), or None when no quest claims it — surfaced
    explicitly rather than silently dropped (No Silent Fallbacks).
    ``resolution`` is an optional human-readable beat/location resolution
    where one is present; the bare anchor list carries none in v1.
    """

    model_config = {"extra": "forbid"}

    anchor_id: str
    quest_id: str | None = None
    resolution: str | None = None


class QuestsPayload(BaseModel):
    """Full quest-spine snapshot (ADR-137 / Story 77-8).

    The RELATIONSHIPS-snapshot analog for the quest spine: log + anchors +
    stakes travel together. An unpopulated spine yields empty lists and an
    empty string — a clean, well-formed empty payload, never None.
    """

    model_config = {"extra": "forbid"}

    quest_log: list[QuestLogEntry] = Field(default_factory=list)
    quest_anchors: list[QuestAnchorEntry] = Field(default_factory=list)
    active_stakes: str = ""


class FateSkillEntry(BaseModel):
    """One skill on the Fate ladder (ADR-144 F3a / Story 118-1).

    Carries both the numeric ``rating`` and its ladder ``ladder`` adjective
    (``fate_resolution.ladder_name``) so the player UI shows the math AND the
    name (Sebastien/Jade legibility). Negative rungs are valid (Terrible -2 ..).
    """

    model_config = {"extra": "forbid"}

    name: str
    rating: int
    ladder: str


class FateAspectEntry(BaseModel):
    """One Fate aspect for the wire (ADR-144 F3a / Story 118-1).

    ``kind`` is the aspect taxonomy (high_concept / trouble / character /
    situation / consequence / boost); ``free_invokes`` is the count of unused
    free invocations the invoke control (Story 118-4) reads.
    """

    model_config = {"extra": "forbid"}

    text: str
    kind: str
    free_invokes: int = 0


class FateStressBox(BaseModel):
    """One checkable stress box of a fixed ``value`` (ADR-144 F3a)."""

    model_config = {"extra": "forbid"}

    value: int
    checked: bool = False


class FateConsequenceEntry(BaseModel):
    """One consequence slot (ADR-144 F3a / Story 118-1).

    ``filled`` is True when the slot has been taken (it then carries the
    consequence's ``text`` as an invokable aspect, SRD); an open slot reads
    ``filled=False`` with an empty ``text``. ``value`` is the SRD absorption
    value for the slot's ``level`` (mild 2 / moderate 4 / severe 6 / extreme 8).
    """

    model_config = {"extra": "forbid"}

    level: str
    value: int
    filled: bool = False
    text: str = ""


class FateStuntEntry(BaseModel):
    """One Fate stunt for the wire (ADR-144 F3a / playtest 150-2).

    A stunt is a named special rule the player picked at chargen; under Fate a PC's
    special abilities ARE their stunts (there is no native class-move surface). The
    ``description`` is the SRD effect text the Character/Fate panel renders.
    ``source_gear`` is the GearDef id this stunt was compiled from (``None`` for a
    hand-picked stunt) — the same traceability the sheet's ``Stunt`` model carries.
    """

    model_config = {"extra": "forbid"}

    name: str
    description: str = ""
    source_gear: str | None = None


class FateCharacterEntry(BaseModel):
    """One PC's full Fate sheet for the wire (ADR-144 F3a / Story 118-1).

    ``aspects`` is the named character aspects only (high_concept / trouble /
    character) — a FILLED consequence is invokable but surfaces in
    ``consequences``, not duplicated here. ``stress`` maps each track name
    (``physical`` / ``mental``) to its ordered boxes. ``stunts`` are the PC's
    chosen stunts — under Fate the player's special abilities ARE their stunts, so
    the Character/Fate panel renders these in place of the native class-move surface
    (playtest 150-2).
    """

    model_config = {"extra": "forbid"}

    name: str
    fate_points: int
    refresh: int
    skills: list[FateSkillEntry] = Field(default_factory=list)
    aspects: list[FateAspectEntry] = Field(default_factory=list)
    stress: dict[str, list[FateStressBox]] = Field(default_factory=dict)
    consequences: list[FateConsequenceEntry] = Field(default_factory=list)
    stunts: list[FateStuntEntry] = Field(default_factory=list)


class FateConflictParticipant(BaseModel):
    """One participant in an active Fate conflict (ADR-144 F3a / Story 118-1).

    ``side`` is the encounter actor's side (``player`` / ``opponent`` /
    ``neutral``).

    ``stress`` / ``consequences`` carry an OPPONENT-side participant's mechanical
    track (playtest 150-2 server follow-up) so the client can draw the opponent
    stress track + the win-condition meter — per ADR-143 the meter is the
    opponent's stress fill toward taken-out, NOT the vestigial native tension dial.
    They reuse the PC sheet's wire shapes (``FateStressBox`` / ``FateConsequenceEntry``).
    A player-side participant leaves both empty: its full sheet already rides in
    ``FateStatePayload.characters``, so the conflict participant never duplicates it.
    """

    model_config = {"extra": "forbid"}

    name: str
    side: str
    stress: dict[str, list[FateStressBox]] = Field(default_factory=dict)
    consequences: list[FateConsequenceEntry] = Field(default_factory=list)


class FatePendingCompel(BaseModel):
    """A narrator-offered compel awaiting the player's accept/refuse (ADR-144 F3e).

    The player-facing projection of ``StructuredEncounter.pending_compels``:
    ``aspect`` the compelled aspect, ``target`` the compelled PC, ``reason`` the
    proposed complication the player reads before deciding. ``offered_delta`` is the
    SRD accept reward (+1) the player gains by accepting — carried on the wire so the
    Accept control renders a real, server-sourced delta instead of a hardcoded label.
    Refuse is the separate SRD-fixed −1 cost, a client-rendered constant (there is no
    stored refuse field — the cost is never variable).
    """

    model_config = {"extra": "forbid"}

    aspect: str
    target: str
    reason: str = ""
    offered_delta: int = 1


class FateConflictEntry(BaseModel):
    """The active Fate conflict's participants by side (ADR-144 F3a).

    ``participants`` is in seating order — the engine's deterministic tiebreak
    order (``fate_opponent._live_player_actors``). Live per-exchange initiative
    (Notice/Empathy) is computed at resolution and surfaces in the F3f overlay,
    not here. ``pending_compels`` (ADR-144 F3e) are the narrator's offered compels
    awaiting accept/refuse — the player surface gates its control on this list.
    """

    model_config = {"extra": "forbid"}

    active: bool = True
    participants: list[FateConflictParticipant] = Field(default_factory=list)
    pending_compels: list[FatePendingCompel] = Field(default_factory=list)


class FateStatePayload(BaseModel):
    """Full Fate-spine snapshot (ADR-144 F3a / Story 118-1).

    The RELATIONSHIPS/QUESTS-snapshot analog for Fate: per-PC sheets + scene
    situation aspects (incl. boosts) + the active conflict travel together. An
    unpopulated payload is a clean empty-but-valid snapshot — never None.
    """

    model_config = {"extra": "forbid"}

    characters: list[FateCharacterEntry] = Field(default_factory=list)
    scene_aspects: list[FateAspectEntry] = Field(default_factory=list)
    conflict: FateConflictEntry | None = None


class FateRollPayload(BaseModel):
    """One resolved 4dF roll, surfaced to the player (ADR-144 F3c / Story 118-3).

    The legibility surface for a Fate action: the four Fudge faces, the ladder
    rating (value + adjective), the shift total, the outcome tier, and a
    succeed-with-style flag. Built from the engine's ``FateOutcome`` (whose dice
    tuple previously reached only the OTEL span) by ``build_fate_roll_payload``.
    A roll is an EVENT, so this rides a dedicated ``FATE_ROLL`` message rather
    than the change-gated ``FATE_STATE`` snapshot.

    ``throw_params`` + ``seed`` mirror ``DiceResultPayload`` (Story 125-4 / ADR-144
    F3g): they let the 3D ``FateDiceTray`` replay-animate the dice instead of
    rendering the idle pickup row. Both are REQUIRED — an optional field would
    silently fall back to the idle (null) render (No Silent Fallbacks).
    """

    model_config = {"extra": "forbid"}

    #: The raw four Fudge faces, each -1 / 0 / +1.
    dice: tuple[int, int, int, int]
    roll_total: int
    ladder_total: int
    #: The Fate ladder adjective for ``ladder_total`` (e.g. "Great").
    ladder_name: str
    opposition: int
    shifts: int
    #: One of Fail / Tie / Succeed / SucceedWithStyle.
    tier: str
    succeeded_with_style: bool
    #: Drag-and-flick gesture for the 3D dice animation (animation only, not
    #: outcome). Mirrors ``DiceResultPayload.throw_params``.
    throw_params: ThrowParams
    #: Deterministic physics seed so every seat replays the same tumble.
    seed: int


class LocationEntityResolution(BaseModel):
    """Result of resolve_location_entity. ADR-109 §5.3.

    ``resolved`` is the single source of truth for the caller. When
    ``resolved=True``, ``entity`` is populated and ``mode_outcome`` records
    which path produced it (matched / promoted / minted). When
    ``resolved=False`` (narrator_proactive miss), ``entity`` is None and
    ``mode_outcome`` is ``"no_match"``.
    """

    model_config = {"extra": "forbid"}

    resolved: bool
    entity: LocationEntity | None = None
    mode_outcome: Literal[
        "matched",  # plain manifest hit, no mutation
        "promoted",  # flavor_only → yes_and, row written
        "minted",  # player_initiated mint, new row written
        "no_match",  # narrator_proactive miss, no mutation
    ]
    region_id: str = Field(min_length=1)
    from_promotion: bool = False


class TokenPayload(ProtocolBase):
    """A token placed on the tactical grid (placeholder — populated at dispatch)."""

    token_id: str
    label: str
    position: tuple[int, int]


class InitiativeEntry(ProtocolBase):
    """One entry in the initiative order (placeholder — populated at dispatch)."""

    token_id: str
    value: int


class TacticalGridPayload(ProtocolBase):
    """Per-room tactical layout for the Map tab. ADR-096.

    Cavern rooms render as a Pillow-rendered PNG floor + token overlay.
    Settlement rooms render as a name/description card; cavern fields are
    None.
    """

    room_id: str
    room_name: str
    room_type: Literal["cavern", "settlement"]

    mask: str | None = None
    """ASCII mask: '.' floor, '#' wall, rows newline-separated. None for settlements."""
    cavern_image_url: str | None = None
    """Resolved (CDN or /genre/) URL for the rendered cavern PNG."""
    cell_size: int | None = None
    cellular: CellularParams | None = None
    derived: DerivedRoomData | None = None

    tokens: list[TokenPayload] = Field(default_factory=list)
    initiative: list[InitiativeEntry] | None = None

    # Settlement-specific fields (ADR-096 Task 20b). Populated from the
    # room YAML for settlement rooms so the UI can render a description
    # card without a separate round-trip. None for cavern rooms.
    settlement_description: str | None = None
    """Human-readable room description from the room YAML. Settlement rooms only."""
    settlement_exits: list[dict] | None = None
    """Exit list from the room YAML, e.g. [{to: "room_id", label: "..."}].
    Settlement rooms only; the Automapper's SettlementRoomView consumes this."""

    entities: list[LocationEntity] = Field(default_factory=list)
    """Typed location-entity manifest per ADR-109. Loaded from the room
    YAML's top-level ``entities`` block. Empty when the room has no
    manifest authored yet — graceful absence, not a lookup failure."""


# ---------------------------------------------------------------------------
# Forward-reference resolution
# ---------------------------------------------------------------------------
#
# ``from __future__ import annotations`` (top of file) makes every annotation a
# lazy string, so ``CharacterSheetDetails.fate_aspects: list[FateAspectEntry]``
# refers to a class defined LATER in this module (Deliverable B1). Rebuild the
# model now that ``FateAspectEntry`` is in module scope so pydantic resolves the
# forward reference. Fail loud if it cannot — a silently-unresolved ref would
# defeat the No Silent Fallbacks rule and surface as a confusing runtime error.
CharacterSheetDetails.model_rebuild()
