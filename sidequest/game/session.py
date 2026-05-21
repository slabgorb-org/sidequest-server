"""Game state composition — GameSnapshot, WorldStatePatch, NpcPatch.

GameSnapshot composes all domain types — serializable for persistence
and WebSocket broadcast.

Phase 1 includes all fields on GameSnapshot to avoid elision, with
comments marking which fields belong to deferred subsystems. Methods
that depend on deferred subsystems (apply_merchant_transactions, etc.)
are stubbed where they would pull in deferred types.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from sidequest.game.belief_state import BeliefState
from sidequest.game.character import Character
from sidequest.game.chassis import ChassisInstance
from sidequest.game.creature_core import (
    CreatureCore,
    Inventory,
    creature_edge_pool_from_hp,
    placeholder_edge_pool,
)
from sidequest.game.disposition import Disposition
from sidequest.game.encounter import StructuredEncounter
from sidequest.game.history_chapter import HistoryChapter
from sidequest.game.lore_store import LoreStore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.resolution_signal import ResolutionSignal
from sidequest.game.resource_pool import (
    NotVoluntary,
    ResourcePatch,
    ResourcePatchOp,
    ResourcePatchResult,
    ResourcePool,
    ResourceThreshold,
    UnknownResource,
    mint_threshold_lore,
)
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.trope_time_skip import TimeSkipBeatEvent
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import ResourceDeclaration
from sidequest.magic.state import MagicState
from sidequest.orbital.course import PlottedCourse

# ---------------------------------------------------------------------------
# NarrativeEntry — narrative log entries
# ---------------------------------------------------------------------------


class NpcEncounterLogTag(BaseModel):
    """NPC encounter tag within a narrative entry (story F3).

    Renamed from ``EncounterTag`` (S4 of the snapshot split-brain cleanup,
    2026-05-04) to disambiguate from
    :class:`sidequest.game.encounter_tag.EncounterTag`, which is a
    different model (scene-momentum tag with leverage/target/fleeting per
    ADR-078). The legacy ``sidequest.game.EncounterTag`` package-level
    alias was removed in story 45-46; import from
    :class:`sidequest.game.NpcEncounterLogTag` (re-exported from this
    module) or directly from :mod:`sidequest.game.session`.
    """

    model_config = {"extra": "forbid"}

    npc_id: str
    encounter_type: str
    archetype_id: str | None = None
    notes: str | None = None


class NarrativeEntry(BaseModel):
    """A single narrative entry in the game log.

    P1-required: narrator reads narrative_log for context.

    Story 45-22: ``author`` MUST be non-blank. Playtest 3 Felix's save
    showed 71 entries all ``author='narrator'`` because the player-turn
    append site was never wired — Sebastien's GM panel could not
    distinguish player input from narrator inference. Rejecting blank
    authors at construction prevents the silent-default failure mode
    AC4 calls out (CLAUDE.md "No Silent Fallbacks").
    """

    model_config = {"extra": "forbid"}

    timestamp: int = 0
    round: int = 0
    # Story 45-22: ``author`` is required (no default) and rejects
    # blank values — the schema is the silent-fallback backstop.
    # Felix's Playtest 3 had 71 entries all author='narrator' because
    # nothing forced the player-turn append site to declare itself.
    author: str
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    encounter_tags: list[NpcEncounterLogTag] = Field(default_factory=list)
    speaker: str | None = None
    entry_type: str | None = None

    @field_validator("author")
    @classmethod
    def author_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "NarrativeEntry.author cannot be blank — "
                "every entry must declare its source (Story 45-22)"
            )
        return v


# ---------------------------------------------------------------------------
# NPC types (minimal — Npc is a deferred full port but needed for GameSnapshot)
# ---------------------------------------------------------------------------


class Npc(BaseModel):
    """Non-player character — minimal Phase 1 port.

    Full enrichment (OCEAN, BeliefState, ResolutionTier) is P5-deferred
    (scenario system). All fields are included so JSON round-trips
    losslessly.
    """

    model_config = {"extra": "forbid"}

    # CreatureCore is nested here for clarity and flattened in persistence.
    core: CreatureCore

    # NPC-specific fields (P1-required: narrator uses name, personality, disposition)
    voice_id: int | None = None
    # ``disposition`` is a ``Disposition`` wrapper (Story 50-10) — exposes
    # ``.value`` (clamped int) and ``.attitude()`` (Attitude enum). The
    # Pydantic schema hook on Disposition coerces raw int construction
    # (``Npc(disposition=15)``) for backward compatibility with existing
    # fixtures and world-materialization callers.
    disposition: Disposition = Field(default_factory=Disposition)
    location: str | None = None
    # Position on a chassis interior (narrator-tracked, optional).
    # Orthogonal to ``location`` (which is general-world); ``current_room``
    # is meaningful only when the NPC is aboard a chassis.
    current_room: str | None = None
    # Wave 2A (story 45-47): pool/state split fields.
    # ``pool_origin`` records the ``NpcPoolMember.name`` this NPC was
    # promoted from, or ``None`` for narrator-invented NPCs. The Sebastien
    # lie-detector signal: per-session counts of ``None`` measure how often
    # the narrator invents off-pool.
    pool_origin: str | None = None
    # ``last_seen_location`` is the location string from the most recent
    # narration that mentioned this NPC. Distinct from ``location`` (current
    # scene location, set when actively framed) and ``current_room`` (chassis
    # interior position). Used by the narrator prompt's NPC roster section
    # for continuity hints.
    last_seen_location: str | None = None
    # Interaction turn of the most recent narration mention. ``0`` means
    # "never mentioned in this session" (turn counter starts at 1).
    last_seen_turn: int = 0
    pronouns: str | None = None
    appearance: str | None = None
    age: str | None = None
    build: str | None = None
    height: str | None = None
    distinguishing_features: list[str] = Field(default_factory=list)

    # P5-deferred: OCEAN personality (story 10-1, scenario system)
    ocean: dict | None = None
    # Scenario system (Epic 7): per-NPC knowledge bubble. Seeded from
    # ScenarioPack.npcs.initial_beliefs at chargen confirmation and
    # mutated between turns by gossip / narrator-driven learning.
    # Gossip + accusation logic defer to a later slice; the data model
    # and mutation surface are live.
    belief_state: BeliefState = Field(default_factory=BeliefState)
    # P2-deferred: ResolutionTier (NPC enrichment system)
    resolution_tier: str = "spawn"
    non_transactional_interactions: int = 0
    # P2-deferred: archetype resolution fields
    jungian_id: str | None = None
    rpg_role_id: str | None = None
    npc_role_id: str | None = None
    resolved_archetype: str | None = None

    # Creature-shape fields (ADR-059 Monster Manual port). Populated when
    # a creature is materialized from a Monster Manual patch; ``None`` /
    # empty for narrator-declared human NPCs. The narrator reads these
    # from the serialized snapshot and treats them as world truth
    # (gaslighting doctrine, see CLAUDE.md).
    creature_id: str | None = None
    """Stable bestiary id this Npc was materialized from. ``None`` for narrator-declared NPCs."""

    threat_level: int | None = None
    """B/X threat tier 1-4. ``None`` for non-creature NPCs."""

    abilities: list[str] = Field(default_factory=list)
    """Creature ability summaries. Empty for non-creature NPCs."""

    morale: str | None = None
    """B/X morale descriptor. ``None`` for non-creature NPCs."""

    def name(self) -> str:
        return self.core.name


class Companion(BaseModel):
    """Narrator-recruited NPC companion (hireling / retainer / ally).

    Playtest 2026-05-06 [BUG]: the narrator was hiring NPCs in prose
    ("Donut joins as torchbearer, half-share contract") but no party
    state mutated, leaving the Party panel showing only the PC. The
    minimum mechanical backing per CLAUDE.md (no narration without
    state) is: name + role + when-recruited, applied via narrator
    sidecar, surfaced in PARTY_STATUS, and traced via OTEL on every
    recruit/dismiss.

    This is intentionally NOT a full character — companions have no
    Edge/inventory/stat block at this tier. They are *roster
    visibility* for the Party panel and *cite-able identity* for the
    narrator. Promotion to a full Character (with Edge bar, inventory,
    confrontation participation) is a separate, larger story.
    """

    model_config = {"extra": "forbid"}

    name: str
    """Display name. Required, non-blank by narrator contract."""
    role: str = ""
    """Hireling role in plain prose: torchbearer, porter, scout, etc."""
    description: str = ""
    """One-sentence narrator-authored description for the panel tooltip."""
    notes: str = ""
    """Optional contract / terms one-liner (\"half-share, surviving recovery\")."""
    recruited_turn: int = 0
    """``snapshot.turn_manager.interaction`` at the moment of recruitment."""
    recruited_by: str = ""
    """Acting PC name on the recruit turn — \"who is this companion bonded to.\""""


class PartyPeer(BaseModel):
    """Canonical identity packet for another party member (not the acting PC).

    Story 37-36: in sealed-letter multiplayer, each player's narrator turn
    must see canonical identity for the *other* PCs — otherwise pronouns
    and race/class drift across saves (playtest 3: Blutka he/him became
    she/her in Orin's save because Orin's narrator had no ground truth).
    Parallels ``NpcPoolMember``, but for peer PCs rather than NPCs.

    Physical identity is canonical (name/pronouns/race/char_class/level).
    Perception — mood, tactics, feelings — stays POV and is not stored here.
    """

    model_config = {"extra": "forbid"}

    name: str
    pronouns: str = ""
    race: str
    char_class: str
    level: int = 1

    @classmethod
    def from_character(cls, character: Character) -> PartyPeer:
        """Project a Character's canonical identity into a PartyPeer packet."""
        return cls(
            name=character.core.name,
            pronouns=character.pronouns,
            race=character.race,
            char_class=character.char_class,
            level=character.core.level,
        )


# ---------------------------------------------------------------------------
# NpcPatch — used in WorldStatePatch.npcs_present
# ---------------------------------------------------------------------------


# Canonical HP→EdgePool translator promoted to
# ``sidequest.game.creature_core.creature_edge_pool_from_hp`` (Beneath
# Sünden Plan 7 Task 4) so the NPC-patch path here and the dungeon
# materializer's CR→Edge seam share ONE implementation. This module-level
# alias preserves the historical ``session._creature_edge_pool_from_hp``
# import (existing call sites + tests) without a second copy of the body.
_creature_edge_pool_from_hp = creature_edge_pool_from_hp


class NpcPatch(BaseModel):
    """Patch for NPC upsert — used in npcs_present.

    Used by two emitters:

    1. The narrator (declared NPCs the prose introduced): name + the
       human-facing flavor fields (description, personality, pronouns,
       appearance, ...).
    2. The Monster Manual pre-narrator seeder (ADR-059, port of
       ``crates/sidequest-server/src/dispatch/pregen.rs``): adds creature
       mechanical fields — ``creature_id``, ``threat_level``, ``hp``,
       ``abilities``, ``morale``. ``hp`` is content-shape B/X HP and is
       translated to an :class:`EdgePool` at materialization
       (``Session._npc_from_patch``) per ADR-078.
    """

    model_config = {"extra": "forbid"}

    name: str
    description: str | None = None
    personality: str | None = None
    role: str | None = None
    pronouns: str | None = None
    appearance: str | None = None
    age: str | None = None
    build: str | None = None
    height: str | None = None
    distinguishing_features: list[str] | None = None
    location: str | None = None

    # Creature-shape fields (ADR-059 Monster Manual port). All optional;
    # narrator-emitted patches leave them None and inherit human-NPC defaults.
    creature_id: str | None = None
    """Stable bestiary id (e.g. ``"chalk_moth"``). Distinguishes creature
    patches from narrator-declared NPC patches at the materializer."""

    threat_level: int | None = None
    """B/X threat tier 1-4 from ``creatures.yaml``. Also flags this patch
    as a creature so the materializer applies hostile-disposition default."""

    hp: int | None = None
    """B/X HP from ``creatures.yaml``. Translated to EdgePool per ADR-078
    when the runtime Npc is built. Content shape, not runtime shape."""

    abilities: list[str] | None = None
    """Creature ability summaries (e.g. ``"Color Feed — Drains pigment..."``)."""

    morale: str | None = None
    """B/X morale descriptor (e.g. ``"cowardly"``, ``"steady"``)."""

    @field_validator("name")
    @classmethod
    def name_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name cannot be blank")
        return v


# ---------------------------------------------------------------------------
# WorldStatePatch
# ---------------------------------------------------------------------------


class DiscoveredFact(BaseModel):
    """A fact discovered by a character this turn (story 9-3).

    P1-required: narrator-delivered facts routed to character known_facts.
    """

    model_config = {"extra": "forbid"}

    character_name: str
    fact: dict  # KnownFact as dict — avoid circular import


class WorldStatePatch(BaseModel):
    """Patch for world-level state (location, atmosphere, quests, regions).

    Only set fields are applied; ``None`` means "no change". Used by
    the narrator agent to update state.
    """

    model_config = {"extra": "forbid"}

    location: str | None = None
    time_of_day: str | None = None
    atmosphere: str | None = None
    quest_log: dict[str, str] | None = None
    quest_updates: dict[str, str] | None = None
    notes: list[str] | None = None
    current_region: str | None = None
    discovered_regions: list[str] | None = None
    discovered_routes: list[str] | None = None
    discover_regions: list[str] | None = None
    discover_routes: list[str] | None = None
    hp_changes: dict[str, int] | None = None
    npc_attitudes: dict[str, int] | None = None
    npcs_present: list[NpcPatch] | None = None
    active_stakes: str | None = None
    lore_established: list[str] | None = None
    discovered_facts: list[DiscoveredFact] | None = None


# ---------------------------------------------------------------------------
# Minimal deferred-subsystem stubs needed as field types in GameSnapshot
# (no logic, only data containers for JSON round-tripping)
# ---------------------------------------------------------------------------


class TropeState(BaseModel):
    """Active trope state (minimal, for JSON round-tripping).

    Story 45-27 adds ``fire_cooldown_until`` and ``last_fired_turn``
    for the cooldown predicate. Cooldown is observed globally
    (`max(t.fire_cooldown_until for t in active_tropes) > now_turn`),
    so per-trope storage is sufficient and round-trips through
    save/reload without a sidecar dict. ``extra="ignore"`` keeps old
    saves loadable when the new fields are absent.
    """

    model_config = {"extra": "ignore"}

    id: str = ""
    status: str = "dormant"
    progress: float = 0.0
    beats_fired: int = 0
    fire_cooldown_until: int | None = None
    last_fired_turn: int | None = None


class SeedGhost(BaseModel):
    """An expired seed trope, retained for cross-session callbacks (Epic 22).

    Record-only and immutable (``frozen=True``): ghosts carry no resolution
    mechanics in this story (22-3). ``extra="ignore"`` keeps old saves loadable.
    """

    model_config = {"extra": "ignore", "frozen": True}

    id: str = ""
    name: str = ""
    expired_at_turn: int = 0
    delivery_hints: list[str] = Field(default_factory=list)


class SeedState(BaseModel):
    """An active seed trope tracked on the snapshot (Epic 22).

    Round-trippable JSON with no side effects on load. ``extra="ignore"``
    mirrors TropeState for forward-compatible saves.
    """

    model_config = {"extra": "ignore"}

    id: str = ""
    name: str = ""
    activated_at_turn: int = 0
    flavor_tags: list[str] = Field(default_factory=list)
    lifespan_turns: int = 0
    delivery_hints: list[str] = Field(default_factory=list)

    def is_expired(self, current_turn: int) -> bool:
        """True once ``current_turn`` reaches the expiry turn (inclusive)."""
        return current_turn >= self.activated_at_turn + self.lifespan_turns

    def to_ghost(self, current_turn: int) -> SeedGhost:
        """Migrate this active seed into a retained ghost record."""
        return SeedGhost(
            id=self.id,
            name=self.name,
            expired_at_turn=current_turn,
            delivery_hints=list(self.delivery_hints),
        )


class GenieWish(BaseModel):
    """Genie wish entry — power-grab with ironic consequences (F9).

    P5-deferred.
    """

    model_config = {"extra": "ignore"}

    wish_text: str = ""
    consequence: str = ""
    status: str = "pending"


class AxisValue(BaseModel):
    """Narrative axis value for /tone command (F2/F10).

    P2-deferred.
    """

    model_config = {"extra": "ignore"}

    axis_id: str = ""
    value: float = 0.0


class AchievementTracker(BaseModel):
    """Achievement tracker (F7) — P6-deferred."""

    model_config = {"extra": "ignore"}

    achievements: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Story 45-13 — Per-room container retrieved-state (Playtest 3 Orin
# regression: same tin box emptied at rounds 10 and 16 because the
# narrator's session memory is not authoritative for mechanical state).
# ContainerState is the explicit lifecycle that replaces "implicit
# narrator session memory" per ADR-014 / ADR-067.
# ---------------------------------------------------------------------------


class ContainerState(BaseModel):
    """Per-container retrieved-state record (Story 45-13).

    Lives inside ``RoomState.containers``. Keyed by narrator-emitted
    container id (e.g. ``"tin_box"``). ``retrieved_at_round`` pairs
    with ``retrieved=True`` so the negative-gate's blocked-span can
    surface a concrete prior round number — the Sebastien lie-detector
    needs the audit trail, not just a bool. The ``retrieved=True with
    retrieved_at_round=None`` state would let the blocked span fire
    with ``prior_retrieved_at_round=0``, which lies about the audit
    trail; the validator below makes that state unrepresentable.
    """

    model_config = {"extra": "ignore"}

    container_id: str
    retrieved: bool = False
    retrieved_at_round: int | None = None

    @model_validator(mode="after")
    def _round_required_when_retrieved(self) -> ContainerState:
        if self.retrieved and self.retrieved_at_round is None:
            raise ValueError("ContainerState.retrieved=True requires retrieved_at_round to be set")
        return self


class RoomState(BaseModel):
    """Per-room mechanical state (Story 45-13).

    Sibling to ``GameSnapshot.discovered_rooms`` (which answers "have we
    been here?") — this answers "what mechanical lifecycle state lives
    here?". Currently holds container retrieval state; trap / lock /
    stochastic-descriptor state are out of scope per the story.
    """

    model_config = {"extra": "ignore"}

    room_id: str
    containers: dict[str, ContainerState] = Field(default_factory=dict)


# ResourcePool lives in sidequest.game.resource_pool (ADR-033).
# Imported at module top for use in ``GameSnapshot.resources`` and the
# patch-application methods below.


# ScenarioState is fully deferred (P5 — Epic 7 / scenario system).
# We use dict | None for the field type to avoid pulling in deferred
# types.


# ---------------------------------------------------------------------------
# GameSnapshot — the complete game state at a point in time
# ---------------------------------------------------------------------------


class GameSnapshot(BaseModel):
    """The complete game state at a point in time.

    Deferred-subsystem fields are present but noted:
    - encounter: typed ``StructuredEncounter | None``. Dispatch-side
      wiring and OTEL emission land in 42-4.
    - active_tropes: P2-deferred (trope engine)
    - campaign_maturity / world_history: P3-deferred (world materialization)
    - genie_wishes: P5-deferred (consequence engine)
    - axis_values: P2-deferred (tone system)
    - achievement_tracker: P6-deferred
    - scenario_state: runtime holder live (Story 2.3 Slice D); gossip/
      accusation logic defers to a later slice
    - discovered_rooms: P3-deferred (room-graph navigation)
    - resources: typed ``dict[str, ResourcePool]`` (ADR-033). Dispatch-
      side wiring and OTEL emission land in 42-4.

    P1-required: genre_slug, world_slug, characters, npcs,
                 character_locations (per-PC scene, Wave 2B / story 45-48),
                 time_of_day, quest_log, notes, narrative_log, atmosphere,
                 current_region, discovered_regions, discovered_routes,
                 turn_manager, active_stakes, lore_established,
                 npc_pool, player_dead.

    Wave 2B (story 45-48): the legacy party-level ``location`` field is
    removed. Use ``self.party_location(perspective=name)`` for single-PC
    framing and ``self.party_location()`` for the consensus view.
    Migration on load (``_migrate_s3_party_location``) promotes any
    legacy ``location`` value into ``character_locations`` for seated PCs
    before pydantic drops the unknown field via ``extra: ignore``.
    """

    model_config = {"extra": "ignore"}  # forward-compat: ignore unknown save fields

    # Session identity (P1-required)
    genre_slug: str = ""
    world_slug: str = ""

    # Core game entities (P1-required)
    characters: list[Character] = Field(default_factory=list)
    npcs: list[Npc] = Field(default_factory=list)

    # World state (P1-required) — Wave 2B (story 45-48): party-level
    # ``location`` field removed; use ``party_location(...)`` accessor
    # or ``character_locations[name]`` for the per-PC source of truth.
    time_of_day: str = ""
    quest_log: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    narrative_log: list[NarrativeEntry] = Field(default_factory=list)

    # StructuredEncounter (ADR-033 confrontation engine) — typed in story 42-1.
    encounter: StructuredEncounter | None = None

    # Spec 2026-05-20 confrontation-intent-validator — directive queue
    # populated by the soft_suggest dispatch branch; consumed and cleared
    # by orchestrator prompt assembly at the start of the next turn.
    # Shared-world by design (a missed confrontation affects the whole
    # table). Per-player scoping would revisit per ADR-037 / ADR-104.
    next_turn_directives: list[str] = Field(default_factory=list)

    # P2-deferred: trope engine state
    active_tropes: list[TropeState] = Field(default_factory=list)

    # Epic 22 — seed trope engine. Active seeds dealt this session and the
    # ghosts of expired ones, both persisted via snapshot_json (ADR-023).
    active_seeds: list[SeedState] = Field(default_factory=list)
    seed_ghosts: list[SeedGhost] = Field(default_factory=list)

    # Story 50-4 — in-game day counter and time-skip beat summary.
    # ``days_elapsed`` is monotonic, advances by clamp(days_advanced, 0, 14)
    # on every narrator turn that emits a multi-day jump. ``pending_time_skip_summary``
    # is a one-shot queue: Pass A2 appends, narrator prompt builder consumes + clears.
    days_elapsed: int = 0
    pending_time_skip_summary: list[TimeSkipBeatEvent] = Field(default_factory=list)

    # World descriptors (P1-required)
    atmosphere: str = ""
    current_region: str = ""
    discovered_regions: list[str] = Field(default_factory=list)
    discovered_routes: list[str] = Field(default_factory=list)

    # Turn tracking (P1-required)
    turn_manager: TurnManager = Field(default_factory=TurnManager)

    # Story-time clock (orbital map / Session aggregate strangler — Task A).
    clock_t_hours: float = Field(
        default=0.0,
        description=(
            "Story-time hours from world epoch. Advanced only via "
            "Session.advance_via_beat (which calls advance_clock_via_beat). "
            "See sidequest.server.session.Session and "
            "sidequest.orbital.clock.Clock."
        ),
    )

    # Party orbital location (orbital map Task 15). Body id from
    # the world's orbits.yaml; distinct from ``location`` which is a
    # free-text place name used in narrator prompts. None when the
    # world has no orbital tier or the party hasn't been placed yet.
    party_body_id: str | None = None

    # Course plot (plot-a-course design). None when no course is plotted.
    # Set by narrator-emitted plot_course sidecar intent; cleared by
    # cancel_course intent, replacement plot, or arrival
    # (party_body_id == plotted_course.to_body_id). Survives save/load.
    plotted_course: PlottedCourse | None = None

    # Quest anchor body ids (plot-a-course MVP — narrator-managed via
    # state patch). Surfaced into compute_courses() as
    # source=QUEST_OBJECTIVE, regardless of orbital scope. Future:
    # superseded by ScenarioState body-anchored clue mechanism.
    quest_anchors: list[str] = Field(default_factory=list)

    # Session metadata
    last_saved_at: datetime | None = None

    # Narrative state (P1-required)
    active_stakes: str = ""
    lore_established: list[str] = Field(default_factory=list)

    # Trope/pacing counters (P2-deferred: trope engagement multiplier)
    turns_since_meaningful: int = 0
    total_beats_fired: int = 0

    # P3-deferred: world materialization (campaign maturity)
    campaign_maturity: str = "Fresh"
    world_history: list[HistoryChapter] = Field(default_factory=list)

    # NPC pool — identity-only members the narrator can cite as
    # "people who exist in this world" (Wave 2A, story 45-47). Replaced
    # the legacy ``npc_registry`` (dropped in story 45-52) as the cast-pool
    # channel. Pool members are promoted to ``Npc`` (in ``self.npcs``) when
    # they engage mechanically; the pool member remains, shadowed by the
    # ``Npc`` lookup at narration_apply time. Legacy saves are migrated
    # via ``_migrate_s2_npc_registry_split`` in ``game/migrations.py``.
    npc_pool: list[NpcPoolMember] = Field(default_factory=list)

    # Companion roster — narrator-recruited hirelings the party
    # currently has on contract. Playtest 2026-05-06 wiring fix.
    # Surfaced in PARTY_STATUS so the Party panel can render the
    # whole party (PCs + companions) and a `party.recruit` /
    # `party.dismiss` watcher span fires on every mutation. Forward-
    # compat: ``model_config = {"extra": "ignore"}`` plus the
    # default-factory means saves predating this field deserialize
    # cleanly with the list empty.
    companions: list[Companion] = Field(default_factory=list)

    # Chassis registry (rig MVP slice — fresh-session only). Materialized from
    # `worlds/<world>/rigs.yaml` at connect time. Wave 2A (story 45-47)
    # removed the projection into the now-dropped ``npc_registry``;
    # chassis surface in the narrator prompt via the dedicated
    # ``register_chassis_voice_section`` (chassis voice zone, separate
    # from the NPC roster).
    chassis_registry: dict[str, ChassisInstance] = Field(default_factory=dict)

    # (S1, 2026-05-04) world_confrontations REMOVED. The duplicate field has
    # been collapsed into magic_state.confrontations. Saves that carried the
    # legacy field are migrated on load by
    # ``sidequest.game.migrations.migrate_legacy_snapshot._migrate_s1_world_confrontations``.

    # Story 47-4: per-(chassis_id, confrontation_id) last-fired turn for the
    # rig-coupled cooldown gate (`fire_conditions.cooldown_turns`). Stored on
    # the snapshot so the cooldown survives serialization. Tuple keys are
    # not JSON-serializable — flat dict keyed by `f"{chassis_id}:{conf_id}"`.
    chassis_autofire_cooldowns: dict[str, int] = Field(default_factory=dict)

    # P5-deferred: genie wishes (consequence engine, F9)
    genie_wishes: list[GenieWish] = Field(default_factory=list)

    # P2-deferred: narrative axis values (tone system, F2/F10)
    axis_values: list[AxisValue] = Field(default_factory=list)

    # P6-deferred: achievement tracker (F7)
    achievement_tracker: AchievementTracker = Field(default_factory=AchievementTracker)

    # Scenario state (Epic 7 — whodunit, belief state, clues). Bound at
    # chargen confirmation when the genre pack declares a scenario
    # (Story 2.3 Slice D). Between-turn processing (gossip, NPC actions,
    # clue availability) and accusation evaluation defer to a later
    # slice — the runtime holder is live now.
    scenario_state: ScenarioState | None = None

    # P3-deferred: room-graph navigation (story 19-2)
    discovered_rooms: list[str] = Field(default_factory=list)

    # Story 45-13 — per-room container retrieved-state. Sibling to
    # ``discovered_rooms`` but answers a different question: "what
    # mechanical lifecycle state lives in this room?" rather than "have
    # we been here?". Keyed by ``snapshot.location`` at the apply site.
    # Forward-compat: ``model_config = {"extra": "ignore"}`` plus the
    # default-factory means old saves serialized without ``room_states``
    # deserialize cleanly with the field empty.
    room_states: dict[str, RoomState] = Field(default_factory=dict)

    # Per-character last-known location (playtest 2026-05-02 [BUG] —
    # multiplayer location header showed peer's scene). The party-level
    # ``location`` is whichever player narrated most recently; in
    # multiplayer that is wrong for any header / projection that needs
    # "where is THIS character right now". Keyed by
    # ``Character.core.name``; populated by
    # ``narration_apply._apply_narration_result_to_snapshot`` whenever
    # the acting player's narration emits a fresh ``location``. Solo
    # sessions populate one entry equal to ``location`` after the first
    # narration; views.py falls back to ``snapshot.location`` when an
    # entry is absent so legacy saves and pre-first-narration MP clients
    # keep their existing behavior.
    character_locations: dict[str, str] = Field(default_factory=dict)

    # Combat state (P1-required: permadeath / death detection)
    player_dead: bool = False

    # Named resource pools (story 42-2 — ADR-033 port)
    resources: dict[str, ResourcePool] = Field(default_factory=dict)

    # Transient resolution signal (Task 14 — dual-track momentum, Phase 1).
    # Set by apply_beat when encounter resolves; read by narrator on next turn
    # to populate [ENCOUNTER RESOLVED] zone; cleared after consumption.
    pending_resolution_signal: ResolutionSignal | None = None

    # Magic system state (Coyote Star iteration 2). None on saves that
    # predate magic or on worlds without a magic config.
    magic_state: MagicState | None = None

    # Phase 5 (Story 47-3): outbound magic-confrontation dispatch queue.
    # Populated by ``narration_apply.apply_magic_working`` (one entry per
    # auto-fire) and ``_resolve_magic_confrontation_if_applicable`` (the
    # outcome resolution). Drained by the websocket session handler
    # after ``_apply_narration_result_to_snapshot`` returns — each entry
    # becomes one outbound ``CONFRONTATION`` or ``CONFRONTATION_OUTCOME``
    # WebSocket frame. Both fields are reset to defaults after the
    # handler dispatches.
    # S5 — Transient outbound dispatch queues. ``exclude=True`` keeps them
    # out of ``model_dump_json``, so a save mid-handler cannot persist a
    # partial queue. They re-initialize empty on load — correct because
    # auto-fires and outcomes are derivable from snapshot state on the
    # next narration turn (``apply_magic_working`` /
    # ``_resolve_magic_confrontation_if_applicable`` recompute from
    # ``magic_state``, not from the queue).
    pending_magic_auto_fires: list[dict] = Field(default_factory=list, exclude=True)
    pending_magic_confrontation_outcome: dict | None = Field(default=None, exclude=True)

    # Multiplayer per-player chargen binding (playtest 2026-04-25). Maps
    # ``player_id`` → ``character.core.name`` so a slug-resume can route
    # an unbound player_id to chargen instead of handing them the first
    # character it finds. Populated by ``_chargen_confirmation`` on
    # commit; consumed by ``_handle_slug_connect`` to decide ``State.Playing``
    # vs ``State.Creating`` per player. Empty on solo / pre-MP saves —
    # the slug-connect path treats an empty map plus non-empty
    # ``characters`` as a single-character resume (back-compat).
    player_seats: dict[str, str] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Legacy save migration (story 42-2 / resource-consolidation phase 4)
    #
    # Old saves stored resources in ``resource_state: dict[str, float]``
    # with metadata in a parallel ``resource_declarations`` vec. New
    # saves store them as ``resources: dict[str, ResourcePool]``. The
    # migration is performed in a ``@model_validator(mode="before")`` so
    # the legacy fields never touch the validated model (they are not
    # declared fields on GameSnapshot).
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_resource_fields(cls, data):
        """Migrate legacy ``resource_state`` + ``resource_declarations`` into ``resources``.

        Precedence:

        1. If ``resources`` is populated in the payload, use it directly
           (new save — takes precedence over any stale legacy fields).
        2. Else if ``resource_state`` is non-empty, synthesize minimal
           :class:`ResourcePool` entries. When a matching entry exists in
           ``resource_declarations``, copy its metadata (label, min, max,
           voluntary, decay_per_turn, thresholds); otherwise produce an
           unbounded pool with empty label that the next
           :meth:`init_resource_pools` call will upsert from the genre pack.
        3. Else leave ``resources`` empty.

        Legacy fields are stripped from the payload so Pydantic does not
        re-ingest them (they are not declared on the model).
        """
        if not isinstance(data, dict):
            return data

        # Always pop legacy fields — they must not reach model validation.
        legacy_state = data.pop("resource_state", None) or {}
        legacy_decls = data.pop("resource_declarations", None) or []
        resources = data.get("resources")

        if resources:
            # New-save path — resources wins outright; legacy fields discarded.
            return data

        if not legacy_state:
            return data

        # Migrate legacy_state → resources, consulting legacy_decls for metadata.
        # Fail loud per CLAUDE.md "No Silent Fallbacks" — malformed legacy
        # payloads must raise, never be dropped.
        import sys as _sys

        decls_by_name: dict[str, dict] = {}
        for d in legacy_decls:
            if not isinstance(d, dict):
                raise ValueError(f"malformed legacy resource_declaration (expected dict): {d!r}")
            decls_by_name[d["name"]] = d

        def _coerce_current(pool_name: str, raw_current: object) -> float:
            try:
                return float(raw_current)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"malformed legacy resource_state[{pool_name!r}]: "
                    f"current must be numeric (got {raw_current!r})"
                ) from e

        migrated: dict[str, dict] = {}
        for name, current in legacy_state.items():
            decl = decls_by_name.get(name)
            if decl is not None:
                migrated[name] = {
                    "name": decl["name"],
                    "label": decl.get("label", ""),
                    "current": _coerce_current(name, current),
                    "min": decl["min"],
                    "max": decl["max"],
                    "voluntary": decl["voluntary"],
                    "decay_per_turn": decl["decay_per_turn"],
                    "thresholds": [
                        {
                            "at": t["at"],
                            "event_id": t["event_id"],
                            "narrator_hint": t["narrator_hint"],
                        }
                        for t in decl.get("thresholds", [])
                    ],
                }
            else:
                # No declaration — synthesize unbounded defaults using
                # ``sys.float_info.max`` (magnitude), negated for the
                # lower bound (the most-negative finite double).
                migrated[name] = {
                    "name": name,
                    "label": "",
                    "current": _coerce_current(name, current),
                    "min": -_sys.float_info.max,
                    "max": _sys.float_info.max,
                    "voluntary": False,
                    "decay_per_turn": 0.0,
                    "thresholds": [],
                }

        data["resources"] = migrated
        return data

    # ------------------------------------------------------------------
    # Derived accessors
    # ------------------------------------------------------------------

    def party_location(self, *, perspective: str | None = None) -> str | None:
        """Resolve "where is the party" from per-character locations (Wave 2B).

        Three modes (per design 2026-05-04 § Wave 2 Story B):

        1. ``perspective`` supplied — returns ``character_locations[perspective]``
           or ``None`` if that character has no entry. Used by single-player
           narrator framing and per-character header rendering.
        2. ``perspective`` omitted, all seated PCs agree on the same location
           — returns that consensus string.
        3. ``perspective`` omitted, seated PCs disagree (or any seated PC
           lacks an entry) — returns ``None``. Callers render "(party split)"
           or equivalent rather than fall back to a stale global.

        Always emits ``snapshot.party_location_query`` for the GM panel
        (lie-detector hook). ``party_split=True`` flags a mid-session
        disagreement; ``party_split=False`` covers both consensus and
        no-party-seated cases (the latter is not a "split" — it's pre-chargen).
        """
        from sidequest.telemetry.spans import SPAN_PARTY_LOCATION_QUERY, Span

        perspective_supplied = perspective is not None

        if perspective_supplied:
            value = self.character_locations.get(perspective) if perspective else None
            with Span.open(
                SPAN_PARTY_LOCATION_QUERY,
                {
                    "perspective_supplied": True,
                    "consensus_found": False,
                    "party_split": False,
                },
            ):
                pass
            return value

        seated = [name for name in self.player_seats.values() if name]
        if not seated:
            with Span.open(
                SPAN_PARTY_LOCATION_QUERY,
                {
                    "perspective_supplied": False,
                    "consensus_found": False,
                    "party_split": False,
                },
            ):
                pass
            return None

        locations = [self.character_locations.get(name) for name in seated]
        if any(loc is None for loc in locations) or len(set(locations)) != 1:
            with Span.open(
                SPAN_PARTY_LOCATION_QUERY,
                {
                    "perspective_supplied": False,
                    "consensus_found": False,
                    "party_split": True,
                },
            ):
                pass
            return None

        consensus = locations[0]
        with Span.open(
            SPAN_PARTY_LOCATION_QUERY,
            {
                "perspective_supplied": False,
                "consensus_found": True,
                "party_split": False,
            },
        ):
            pass
        return consensus

    # ------------------------------------------------------------------
    # State mutation methods
    # ------------------------------------------------------------------

    def replace_with(self, other: GameSnapshot) -> None:
        """Copy every field of ``other`` onto this snapshot in place.

        Used when the chargen-complete pipeline materializes a fresh
        world from the genre pack and needs to install it into the
        canonical room snapshot without orphaning the room's reference.

        ADR-037 (Python port) requires that the room owns the canonical
        ``GameSnapshot`` and every WS session bound to the slug holds
        the same object. Reassigning ``sd.snapshot = materialized``
        violates that invariant: the session's pointer moves, the
        room's pointer doesn't, and ``room.save()`` then persists the
        stale (empty) original. Symptom: a second player joining the
        slug loads from disk, sees no characters, and treats themselves
        as the first commit — two parallel solo games on one slug.

        Mutating in place keeps ``id(self)`` stable, so all existing
        references stay live.
        """
        for name in type(other).model_fields:
            setattr(self, name, getattr(other, name))

    def record_beat_fired(
        self,
        *,
        beat_id: str,
        encounter_type: str | None,
        turn: int,
        source: str,
    ) -> int:
        """Increment ``total_beats_fired`` and emit an OTEL watcher event.

        Call this after every successful ``apply_beat`` (i.e., when
        ``ApplyResult.skipped_reason is None``). Returns the new counter
        value.

        Story 45-9: counter was defined but never bumped, so any
        beat-gated unlock (campaign maturity tiers in
        ``world_materialization.derive_maturity``) silently never opened.
        Fix is unconditional bump on each successful fire — no silent
        fallbacks (CLAUDE.md). The OTEL ``beat_fired`` event lets the GM
        panel verify the counter is moving rather than trusting the
        narration.
        """
        # Lazy import — telemetry depends on game models, so a top-level
        # import would invert the dependency.
        from sidequest.telemetry.watcher_hub import (
            publish_event as _watcher_publish,
        )

        self.total_beats_fired += 1
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "beat_fired",
                "beat_id": beat_id,
                "encounter_type": encounter_type or "",
                "turn": turn,
                "source": source,
                "total_beats_fired": self.total_beats_fired,
            },
            component="encounter",
        )
        return self.total_beats_fired

    def apply_world_patch(self, patch: WorldStatePatch) -> None:
        """Apply a world state patch. Only set fields are updated."""
        from sidequest.telemetry.spans import SPAN_APPLY_WORLD_PATCH, Span

        with Span.open(
            SPAN_APPLY_WORLD_PATCH,
            {
                "field_count": sum(
                    1 for f in patch.model_fields_set if getattr(patch, f, None) is not None
                ),
            },
        ):
            self._apply_world_patch_inner(patch)

    def _apply_world_patch_inner(self, patch: WorldStatePatch) -> None:
        if patch.location is not None:
            # Wave 2B (story 45-48): no party-level snapshot.location to
            # write. ``WorldStatePatch.location`` is a party-frame intent
            # ("everyone is now here") — apply to every seated PC's
            # ``character_locations`` entry. Solo and pre-MP saves end up
            # with one entry; MP gets all seats. Pre-chargen patches with
            # no seated PCs fall back to writing under the first character
            # name we find (legacy behavior preservation for tests that
            # apply a patch before chargen is wired).
            seated = [name for name in self.player_seats.values() if name]
            if seated:
                for name in seated:
                    self.character_locations[name] = patch.location
            elif self.characters:
                for character in self.characters:
                    self.character_locations[character.core.name] = patch.location
        if patch.time_of_day is not None:
            self.time_of_day = patch.time_of_day
        if patch.atmosphere is not None:
            self.atmosphere = patch.atmosphere
        if patch.quest_log is not None:
            self.quest_log = patch.quest_log
        if patch.quest_updates is not None:
            self.quest_log.update(patch.quest_updates)
        if patch.notes is not None:
            self.notes = patch.notes
        if patch.current_region is not None:
            # The REAL production region-transition point (ADR-011
            # WorldStatePatch apply — the only code that mutates
            # current_region mid-session). Beneath Sünden Plan 7 Task 6
            # hooks the frontier-crossing / frontier-approach seam here
            # (extending this point + ADR-055 region_init dedup-append,
            # NOT a parallel nav path). Fire only on a genuine region
            # change (different, non-empty target) so the look-ahead is
            # never spuriously enqueued (No Silent Fallbacks). Lazy import
            # — sidequest.dungeon depends on game models, so a top-level
            # import would invert the dependency (the watcher_hub
            # lazy-import precedent two methods up).
            _prev_region = self.current_region
            self.current_region = patch.current_region
            if patch.current_region and patch.current_region != _prev_region:
                from sidequest.dungeon.frontier_hook import (
                    notify_region_transition,
                )

                notify_region_transition(
                    self,
                    from_region=_prev_region or None,
                    to_region=patch.current_region,
                )
        if patch.discovered_regions is not None:
            # Stories 45-16 + 45-17: validate, then canonicalize-dedup.
            # 45-16 rejected non-room shapes (brackets, multiline);
            # 45-17 collapses surface variants of the same room
            # ("The Crew Quarters" vs "the crew quarters") so the
            # wholesale-replace path can't smuggle in dups either.
            from sidequest.game.region_validation import (
                canonicalize_region_name,
                validate_region_name,
            )
            from sidequest.telemetry.spans import (
                region_entry_canonicalized_dedup_span,
                region_entry_rejected_span,
            )

            filtered: list[str] = []
            seen_slugs: dict[str, str] = {}  # slug → first surface form kept
            for r in patch.discovered_regions:
                ok, reason = validate_region_name(r)
                if not ok:
                    with region_entry_rejected_span(
                        entry=r if isinstance(r, str) else repr(r),
                        reason=reason or "unknown",
                        caller_path="session.apply_patch.discovered_regions_set",
                    ):
                        pass
                    continue
                slug = canonicalize_region_name(r)
                existing = seen_slugs.get(slug)
                if existing is None:
                    seen_slugs[slug] = r
                    filtered.append(r)
                elif existing != r:
                    with region_entry_canonicalized_dedup_span(
                        entry=r,
                        canonical_slug=slug,
                        existing_surface_form=existing,
                        caller_path="session.apply_patch.discovered_regions_set",
                    ):
                        pass
            self.discovered_regions = filtered
        if patch.discovered_routes is not None:
            self.discovered_routes = patch.discovered_routes
        if patch.discover_regions is not None:
            from sidequest.game.region_validation import (
                canonicalize_region_name,
                validate_region_name,
            )
            from sidequest.telemetry.spans import (
                region_entry_canonicalized_dedup_span,
                region_entry_rejected_span,
            )

            for r in patch.discover_regions:
                ok, reason = validate_region_name(r)
                if not ok:
                    with region_entry_rejected_span(
                        entry=r if isinstance(r, str) else repr(r),
                        reason=reason or "unknown",
                        caller_path="session.apply_patch.discover_regions",
                    ):
                        pass
                    continue
                # Story 45-17: canonical-dedup against existing list
                # so incremental discoveries don't accumulate
                # surface-variant duplicates.
                new_slug = canonicalize_region_name(r)
                existing_match: str | None = None
                for existing in self.discovered_regions:
                    if canonicalize_region_name(existing) == new_slug:
                        existing_match = existing
                        break
                if existing_match is None:
                    self.discovered_regions.append(r)
                elif existing_match != r:
                    with region_entry_canonicalized_dedup_span(
                        entry=r,
                        canonical_slug=new_slug,
                        existing_surface_form=existing_match,
                        caller_path="session.apply_patch.discover_regions",
                    ):
                        pass
        if patch.discover_routes is not None:
            for r in patch.discover_routes:
                if r not in self.discovered_routes:
                    self.discovered_routes.append(r)
        if patch.active_stakes is not None:
            self.active_stakes = patch.active_stakes
        if patch.lore_established is not None:
            self.lore_established.extend(patch.lore_established)
        if patch.discovered_facts is not None:
            from sidequest.game.character import KnownFact

            for df in patch.discovered_facts:
                for ch in self.characters:
                    if ch.core.name == df.character_name:
                        kf = KnownFact.model_validate(df.fact)
                        ch.known_facts.append(kf)
        if patch.hp_changes is not None:
            for name, delta in patch.hp_changes.items():
                self._apply_hp_change(name, delta)
        if patch.npc_attitudes is not None:
            from sidequest.telemetry.spans import SPAN_DISPOSITION_SHIFT, Span

            for name, delta in patch.npc_attitudes.items():
                for npc in self.npcs:
                    if npc.core.name == name:
                        before = int(npc.disposition)
                        # Disposition clamps to ±100 in its constructor.
                        npc.disposition = Disposition(before + delta)
                        after = int(npc.disposition)
                        # Emit plain str values into span attributes so the
                        # OTEL wire form is a JSON string, not the enum repr.
                        before_attitude = Disposition(before).attitude().value
                        after_attitude = npc.disposition.attitude().value
                        # Real span (was Emitter.fire add_event — sprint 3 cold-subsystem
                        # audit). add_event decorates the parent span only;
                        # WatcherSpanProcessor sees span closes, so the GM panel never
                        # received affinity shifts. A zero-duration span hops through
                        # SPAN_ROUTES[disposition.shift] → state_transition. Story 50-11
                        # adds the threshold-crossing fields so the GM panel can tell
                        # band flips (neutral→friendly) from intra-band drift; `crossed`
                        # is derived from band identity, not |delta| vs a literal, so
                        # 50-13's genre-configurable thresholds can land without
                        # revisiting this callsite.
                        with Span.open(
                            SPAN_DISPOSITION_SHIFT,
                            {
                                "npc_name": name,
                                "delta": int(delta),
                                "before": before,
                                "after": after,
                                "before_attitude": before_attitude,
                                "after_attitude": after_attitude,
                                "crossed": before_attitude != after_attitude,
                            },
                        ):
                            pass
        if patch.npcs_present is not None:
            for npc_patch in patch.npcs_present:
                existing = next((n for n in self.npcs if n.core.name == npc_patch.name), None)
                if existing is not None:
                    self._merge_npc_patch(existing, npc_patch)
                else:
                    self.npcs.append(self._npc_from_patch(npc_patch))

    def _apply_hp_change(self, name: str, delta: int) -> None:
        for ch in self.characters:
            if ch.core.name == name:
                ch.core.apply_edge_delta(delta)
                return
        for npc in self.npcs:
            if npc.core.name == name:
                npc.core.apply_edge_delta(delta)
                return

    def find_creature_core(self, name: str) -> CreatureCore | None:
        """Resolve an actor name (PC or NPC) to its ``CreatureCore``.

        Used as the ``edge_resolver`` callable for ``apply_beat`` and
        ``resolve_opposed_check``: both need to read or mutate the live
        edge pool of an actor named in an encounter selection. Returns
        ``None`` when the name doesn't match any seated character or
        instantiated NPC — caller decides whether that's a hard error
        (target_edge_delta with no actor) or a legitimate omission
        (numerical_advantage_for excludes unknown allies).
        """
        for ch in self.characters:
            if ch.core.name == name:
                return ch.core
        for npc in self.npcs:
            if npc.core.name == name:
                return npc.core
        return None

    def _merge_npc_patch(self, npc: Npc, patch: NpcPatch) -> None:
        if patch.description is not None:
            npc.core.description = patch.description
        if patch.personality is not None:
            npc.core.personality = patch.personality
        if patch.pronouns is not None:
            npc.pronouns = patch.pronouns
        if patch.appearance is not None:
            npc.appearance = patch.appearance
        if patch.age is not None:
            npc.age = patch.age
        if patch.build is not None:
            npc.build = patch.build
        if patch.height is not None:
            npc.height = patch.height
        if patch.distinguishing_features is not None:
            npc.distinguishing_features = patch.distinguishing_features
        if patch.location is not None:
            npc.location = patch.location

        # Creature-shape fields (ADR-059): patches re-emitted on
        # re-encounter (e.g. Monster Manual seed during a save+load
        # roundtrip) update creature flavor and re-derive the edge pool
        # from the latest hp claim.
        if patch.creature_id is not None:
            npc.creature_id = patch.creature_id
        if patch.threat_level is not None:
            npc.threat_level = patch.threat_level
        if patch.abilities is not None:
            npc.abilities = list(patch.abilities)
        if patch.morale is not None:
            npc.morale = patch.morale
        if patch.hp is not None:
            npc.core.edge = _creature_edge_pool_from_hp(patch.hp)

    def _npc_from_patch(self, patch: NpcPatch) -> Npc:
        # Creature signal: presence of any creature-shape field flags this
        # as a Monster Manual patch (ADR-059). Translate B/X hp → EdgePool
        # per ADR-078 and default hostile disposition matching encountergen.
        is_creature = (
            patch.creature_id is not None or patch.threat_level is not None or patch.hp is not None
        )
        if patch.hp is not None:
            edge = _creature_edge_pool_from_hp(patch.hp)
        else:
            edge = placeholder_edge_pool()

        core = CreatureCore(
            name=patch.name,
            description=patch.description or "No description",
            personality=patch.personality or "Unknown",
            level=patch.threat_level if patch.threat_level is not None else 1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            edge=edge,
        )
        return Npc(
            core=core,
            pronouns=patch.pronouns,
            appearance=patch.appearance,
            age=patch.age,
            build=patch.build,
            height=patch.height,
            distinguishing_features=patch.distinguishing_features or [],
            location=patch.location,
            # Creatures default to hostile (-20), matching encountergen output.
            disposition=-20 if is_creature else 0,
            creature_id=patch.creature_id,
            threat_level=patch.threat_level,
            abilities=list(patch.abilities) if patch.abilities is not None else [],
            morale=patch.morale,
        )

    def lowest_friendly_hp_ratio(self) -> float:
        """Lowest edge fraction among friendly characters. Returns 1.0 if none."""
        fracs = [ch.edge_fraction() for ch in self.characters if ch.is_friendly]
        return min(fracs) if fracs else 1.0

    # ------------------------------------------------------------------
    # Resource pool mutation (ADR-033)
    #
    # These methods are the public surface for ResourcePool mutation;
    # the per-pool clamp + crossing-detection primitive lives in
    # ``ResourcePool._apply_and_clamp`` and is the single invariant-
    # enforcing path for all pool mutation (including decay).
    #
    # OTEL: span emission for resource-pool mutations is deferred to
    # story 42-4 (dispatch + OTEL). See context-epic-42.md. The GM-panel
    # lie-detector picks up these methods once 42-4 wires the spans.
    # ------------------------------------------------------------------

    def apply_resource_patch(self, patch: ResourcePatch) -> ResourcePatchResult:
        """Apply a resource patch (engine-level — ignores ``voluntary`` flag).

        Raises :class:`UnknownResource` if no pool matches
        ``patch.resource_name``.
        """
        pool = self.resources.get(patch.resource_name)
        if pool is None:
            raise UnknownResource(patch.resource_name)
        return pool._apply_and_clamp(patch.operation, patch.value)

    def apply_resource_patch_player(self, patch: ResourcePatch) -> ResourcePatchResult:
        """Apply a resource patch as a player action.

        Rejects ``Subtract`` against non-voluntary pools with
        :class:`NotVoluntary`; ``Add`` and ``Set`` bypass the voluntary
        check (voluntary only gates player-initiated spend). Raises
        :class:`UnknownResource` if no pool matches
        ``patch.resource_name``.
        """
        if patch.operation is ResourcePatchOp.Subtract:
            pool = self.resources.get(patch.resource_name)
            if pool is None:
                raise UnknownResource(patch.resource_name)
            if not pool.voluntary:
                raise NotVoluntary(patch.resource_name)
        return self.apply_resource_patch(patch)

    def apply_pool_decay(self) -> list[ResourceThreshold]:
        """Apply ``decay_per_turn`` to all resource pools.

        Skips pools whose ``decay_per_turn`` is effectively zero
        (``abs < sys.float_info.epsilon``). Routes each non-zero decay
        through :meth:`ResourcePool._apply_and_clamp` so clamp +
        crossing detection share the same invariant-enforcing primitive
        as the patch path. Returns a flat list of all thresholds
        crossed across all pools this tick (also available via
        :attr:`ResourcePatchResult.crossed_thresholds` per-pool).
        """
        import sys

        all_crossings: list[ResourceThreshold] = []
        eps = sys.float_info.epsilon
        for pool in self.resources.values():
            if abs(pool.decay_per_turn) < eps:
                continue
            result = pool._apply_and_clamp(ResourcePatchOp.Add, pool.decay_per_turn)
            all_crossings.extend(result.crossed_thresholds)
        return all_crossings

    def init_resource_pools(self, declarations: list[ResourceDeclaration]) -> None:
        """Initialize or upsert resource pools from genre pack declarations.

        Upsert semantics (critical for save migration):

        - If a pool with this name already exists (e.g., from a loaded save),
          update its declaration-derived fields (``label``, ``min``, ``max``,
          ``voluntary``, ``decay_per_turn``, ``thresholds``) but **preserve
          the existing** ``current``. Re-clamp ``current`` into the possibly
          new bounds.
        - If no pool exists, create a new one with ``current = decl.starting``.

        This is what makes old saves migrate correctly: the deserializer
        creates minimal :class:`ResourcePool` entries with the saved
        ``current``, then this call populates the genre-pack metadata
        without clobbering the player's progress.
        """
        for decl in declarations:
            thresholds = [
                ResourceThreshold(
                    at=t.at,
                    event_id=t.event_id,
                    narrator_hint=t.narrator_hint,
                )
                for t in decl.thresholds
            ]
            existing = self.resources.get(decl.name)
            if existing is not None:
                # Preserve ``current`` — refresh everything else from pack.
                existing.label = decl.label
                existing.min = decl.min
                existing.max = decl.max
                existing.voluntary = decl.voluntary
                existing.decay_per_turn = decl.decay_per_turn
                existing.thresholds = thresholds
                # Re-clamp in case the new bounds invalidate the saved value.
                existing.current = max(existing.min, min(existing.max, existing.current))
            else:
                self.resources[decl.name] = ResourcePool(
                    name=decl.name,
                    label=decl.label,
                    current=decl.starting,
                    min=decl.min,
                    max=decl.max,
                    voluntary=decl.voluntary,
                    decay_per_turn=decl.decay_per_turn,
                    thresholds=thresholds,
                )

    def apply_resource_patch_by_name(
        self,
        name: str,
        op: ResourcePatchOp,
        value: float,
    ) -> ResourcePatchResult:
        """Convenience: apply a resource patch by name, op, and value."""
        return self.apply_resource_patch(
            ResourcePatch(resource_name=name, operation=op, value=value)
        )

    def process_resource_patch_with_lore(
        self,
        name: str,
        op: ResourcePatchOp,
        value: float,
        store: LoreStore,
        turn: int,
    ) -> ResourcePatchResult:
        """Apply a resource patch and mint LoreFragments for crossings.

        Story 16-11. Threshold crossings are minted into ``store`` via
        :func:`mint_threshold_lore`; duplicate event_ids are idempotent.
        """
        result = self.apply_resource_patch_by_name(name, op, value)
        mint_threshold_lore(result.crossed_thresholds, store, turn)
        return result
