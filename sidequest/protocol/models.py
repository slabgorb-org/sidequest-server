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
# CharacterSheetDetails — full character sheet nested inside PartyMember
# ---------------------------------------------------------------------------


class CharacterSheetDetails(ProtocolBase):
    """Character sheet details nested inside PartyMember.

    Port of sidequest_protocol::CharacterSheetDetails.
    """

    race: NonBlankString
    """Character race/origin. Non-blank post-chargen."""
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


# ---------------------------------------------------------------------------
# PartyMember — character party snapshot
# ---------------------------------------------------------------------------


class PartyMember(ProtocolBase):
    """A party member in PARTY_STATUS.

    Port of sidequest_protocol::PartyMember.
    """

    player_id: NonBlankString
    """Player identifier. Non-blank — identity key."""
    name: NonBlankString
    """Player lobby name. Non-blank."""
    character_name: NonBlankString | None = None
    """In-game character name. Optional (None = still in chargen)."""
    current_hp: int
    """Current HP."""
    max_hp: int
    """Maximum HP."""
    statuses: list[str]
    """Active statuses."""
    class_: NonBlankString = Field(alias="class")
    """Character class. Non-blank."""
    level: int
    """Character level."""
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
    prose: str
    terrain: str | None = None
    entities: list[LocationEntity] = Field(default_factory=list)
    overlays: list[LocationDescriptionOverlaySummary] = Field(default_factory=list)
    # Story 63-6: deep-link from the region header into the /reference/lore
    # wiki. None when the region has no lore-page anchor (region-mode worlds,
    # old snapshots) — the UI then renders the header as plain text.
    reference_url: str | None = None


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
