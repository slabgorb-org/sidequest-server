"""Phase 1 GameMessage payloads and the GameMessage discriminated union.

Port of the payload structs and GameMessage enum from
sidequest-protocol/src/message.rs (Phase 1 subset — 12 message types).

Wire format: Rust uses #[serde(tag = "type")] with struct variants.
Each serialized message has "type" at the top level, plus "payload" and
"player_id" as sibling fields:

    {"type": "PLAYER_ACTION", "payload": {"action": "...", "aside": false}, "player_id": ""}

GameMessage is modelled as a pydantic discriminated union using Annotated +
Field(discriminator="type"). Each concrete message class carries a Literal[MessageType]
discriminator, a payload field, and a player_id field.

Payloads with deny_unknown_fields in Rust use model_config = {"extra": "forbid"} here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, RootModel, model_validator

from sidequest.protocol.base import ProtocolBase
from sidequest.protocol.dice import (
    DiceRequestPayload,
    DiceResultPayload,
    DiceThrowPayload,
)
from sidequest.protocol.enums import MessageType, NarratorVerbosity, NarratorVocabulary
from sidequest.protocol.models import (
    ClassRequirement,
    CompanionMember,
    CreationChoice,
    Footnote,
    InitialState,
    JournalEntry,
    LocationDescriptionPayload,
    LocationOverlayChangedPayload,
    PartyMember,
    QuestsPayload,
    RelationshipsPayload,
    RolledStat,
    StateDelta,
    TacticalGridPayload,
)
from sidequest.protocol.orbital_intent import OrbitalIntent, OrbitalIntentResponse
from sidequest.protocol.types import NonBlankString

# ---------------------------------------------------------------------------
# PlayerActionPayload
# ---------------------------------------------------------------------------


class PlayerActionPayload(ProtocolBase):
    """Player action payload.

    Port of sidequest_protocol::PlayerActionPayload.
    """

    action: NonBlankString
    """The action text the player typed. Non-blank."""
    aside: bool = False
    """True if this is an out-of-character aside."""
    round: int = Field(ge=0)
    """Round (ADR-051) the action was submitted in. Required and non-negative —
    a missing round fails loud (No Silent Fallbacks) rather than letting a client
    silently default to round 0. Consumed UI-side: the client's own buildSegments
    anchors the peer-action transcript by this round (Story 71-10). The server
    validates the field but does not read it; outbound round stamps use the
    authoritative ``turn_manager.round``."""


# ---------------------------------------------------------------------------
# NarrationPayload
# ---------------------------------------------------------------------------


class NarrationPayload(ProtocolBase):
    """Narration payload with optional state delta and structured footnotes.

    Port of sidequest_protocol::NarrationPayload.
    """

    text: NonBlankString
    """The narrative text from the AI. Non-blank."""
    state_delta: StateDelta | None = None
    """Optional state changes resulting from this narration."""
    footnotes: list[Footnote] = Field(default_factory=list)
    """Structured footnotes — new discoveries and callbacks to prior knowledge."""
    seq: int = 0
    """Event-log sequence number assigned when this narration was persisted (MP-03 Task 3).
    Clients use this value as last_seen_seq on reconnect to catch up on missed events."""
    visibility_sidecar: dict | None = Field(default=None, alias="_visibility")
    """Aggregated VisibilityTag sidecar.

    v1 shape (Group G Task 4):
        ``{"visible_to": ["player:Alice"] | "all", "fidelity": {entity_id: fidelity_level}}``

    v2 shape (Story 49-8 — purely additive):
        ``{"visible_to": ..., "fidelity": ..., "anchor_pc": "Carl" | None,
          "pov_strategy": "pc_anchored" | "atmospheric" | "private"}``

    Producers:
      - :func:`sidequest.server.session_helpers.aggregate_visibility` (v1
        — DispatchPackage path).
      - :func:`sidequest.server.visibility_classifier.classify_narration_visibility`
        (v2 — post-narration emit path).

    Consumers:
      - :class:`sidequest.game.projection.rules.VisibilityTagRule` reads
        ``visible_to`` and ``fidelity`` (ignores v2 keys it does not know
        about — additivity is load-bearing).
      - :func:`sidequest.server.emitters._apply_pov_swap` reads
        ``anchor_pc`` + ``pov_strategy`` to rewrite the prose into
        2nd-person for the recipient whose PC matches the anchor.

    Wire name is ``_visibility`` to signal "sidecar / out-of-band" to
    downstream consumers; full ``alias`` (not just
    ``serialization_alias``) so that event-log replay — which
    deserializes the wire-format payload — round-trips correctly."""


# ---------------------------------------------------------------------------
# NarrationSegmentPayload (ADR-105 B3)
# ---------------------------------------------------------------------------


class NarrationSegmentPayload(ProtocolBase):
    """A single PC's private narration prose (ADR-105 B3).

    The shared :class:`NarrationPayload` ``text`` is public-safe by the
    amended SDK-narrator output contract — only prose every present PC
    can observe. Any genuinely PC-private perception (a withheld arcane
    probe result, a traitor's secret briefing, a blinded PC's
    sound-only read) is partitioned by the narrator at generation time
    into one of these segments and emitted as its own ``NARRATION_SEGMENT``
    event.

    Routing: ``_visibility.visible_to`` carries the single owning PC's
    player_id (GM is an implicit recipient via the GM CoreInvariant).
    There is intentionally **no** ``to`` field — the structural
    visibility-gated CoreInvariant (ADR-105 B1) reads
    ``_visibility.visible_to`` and a pack cannot weaken it. ``anchor_pc``
    is the owning PC's name so the per-segment POV swap (ADR-105 B4)
    can rewrite it to 2nd-person for that recipient.
    """

    text: NonBlankString
    """The private prose — that PC's perception only. Non-blank: an
    empty private segment is meaningless and must fail loud upstream."""
    anchor_pc: str | None = None
    """The PC name who alone perceives this segment. Drives the
    per-segment 2nd-person POV swap (ADR-105 B4)."""
    turn_id: str = ""
    """Originating turn id — lets the UI thread the segment under its
    public NARRATION card."""
    visibility_sidecar: dict | None = Field(default=None, alias="_visibility")
    """Wire name ``_visibility``; same shape as
    :attr:`NarrationPayload.visibility_sidecar`. ``visible_to`` is the
    single owning PC's player_id list. Full ``alias`` (not
    ``serialization_alias``) so event-log replay round-trips cleanly."""
    seq: int = 0
    """Event-log sequence number assigned when the segment is persisted."""


# ---------------------------------------------------------------------------
# SpokenLinePayload (playtest 2026-05-17 — MP dialogue visibility)
# ---------------------------------------------------------------------------


class ClientErrorPayload(ProtocolBase):
    """A client-side render crash report (Story 67-1).

    Sent by an ErrorBoundary catch over the still-open socket so the server
    can release the crashed player from the turn barrier. ``reason`` is a
    coarse machine tag ("render_crash"); ``component`` names the boundary
    that caught it ("GameBoard"). Both feed the ``mp.player_crash_released``
    OTEL span so the GM panel can see exactly what released the barrier.
    """

    reason: str = "render_crash"
    """Coarse machine-readable cause tag. Default covers the only sender today."""
    component: str = ""
    """The boundary/component name that caught the crash (e.g. "GameBoard")."""


class SpokenLinePayload(ProtocolBase):
    """One verbatim line a PC spoke aloud, attributed to that PC.

    Surfaced into the shared transcript at barrier-fire so every player
    sees what each PC said to NPCs/each other. The narrator must not
    echo player speech (SOUL.md Agency) and ``ACTION_REVEAL`` is wiped
    on dispatch, so without this the words vanished for peers (Keith +
    Sebby, 2026-05-17). This is public table speech — broadcast to all,
    deliberately NOT routed through the ADR-105 perception firewall or
    the POV swap (it stays "Rux: ..." verbatim, not 2nd-person).
    """

    character_name: str
    """The PC who spoke. Empty only if seat resolution failed upstream."""
    text: NonBlankString
    """The verbatim spoken line. Non-blank — empty utterances are
    dropped by ``extract_spoken_lines`` before a payload is built."""
    round: int = 0
    """Round number, stamped server-side for client transcript ordering."""


# ---------------------------------------------------------------------------
# SecretNotePayload (Group G Task 6)
# ---------------------------------------------------------------------------


class SecretNotePayload(ProtocolBase):
    """Per-recipient note derived from a prompt-redacted SubsystemDispatch.

    Group G Task 6. When Task 5's ``redact_dispatch_package`` strips entries
    marked ``redact_from_narrator_canonical=True`` from the narrator prompt,
    the session handler routes each stripped ``SubsystemDispatch`` as a
    SECRET_NOTE event through the same EventLog + ProjectionFilter pipeline
    as NARRATION. The ``visibility_tag`` rule (Task 3) then delivers the
    SECRET_NOTE only to recipients in ``_visibility.visible_to``.
    """

    turn_id: str
    """DispatchPackage turn id this note belongs to."""
    idempotency_key: str
    """Matches the originating SubsystemDispatch.idempotency_key."""
    subsystem: str
    """Subsystem name from the originating dispatch."""
    params: dict = Field(default_factory=dict)
    """Opaque params from the originating dispatch."""
    visibility_sidecar: dict | None = Field(default=None, alias="_visibility")
    """Wire name is ``_visibility``; same shape as NarrationPayload.visibility_sidecar:
    ``{"visible_to": ["player:Alice"], "fidelity": {entity_id: fidelity_level}}``.
    Full ``alias`` (not ``serialization_alias``) so event-log replay round-trips cleanly."""
    seq: int = 0
    """Event-log sequence number assigned when the note is persisted."""


# ---------------------------------------------------------------------------
# ScrapbookEntryPayload (pingpong 2026-04-26 [S3-REGRESSION])
# ---------------------------------------------------------------------------


class ScrapbookEntryNpcRef(ProtocolBase):
    """Light-weight NPC reference embedded in a scrapbook entry."""

    name: str
    role: str = "neutral"
    disposition: str = ""
    # Story 65-6: world-scoped portrait URL, attached when the invoked NPC
    # matches a portrait_manifest entry for the current world. ``None`` when
    # the NPC has no authored portrait (the common case — most invoked NPCs
    # are ad-hoc). The UI renders a thumbnail next to the name when present.
    portrait_url: str | None = None


class ScrapbookEntryPayload(ProtocolBase):
    """Server-authored scrapbook entry — emitted once per narration turn."""

    turn_id: int
    location: str
    narrative_excerpt: str
    scene_title: str | None = None
    scene_type: str | None = None
    image_url: str | None = None
    world_facts: list[str] = Field(default_factory=list)
    npcs_present: list[ScrapbookEntryNpcRef] = Field(default_factory=list)
    seq: int = 0
    # Trigger-policy outcome (Story 45-30) extended with daemon-liveness
    # outcome (Story 45-31). Per 45-31 spec: "render_status field is shared
    # with story 45-30. If 45-30 lands first, this story extends the enum
    # with 'unavailable'." 45-30 landed first.
    #   ``rendered``       — policy fired, dispatch proceeded, image landed.
    #   ``skipped_policy`` — classify_trigger returned NONE_POLICY (banter).
    #   ``failed``         — policy fired, daemon gate refused synchronously.
    #   ``unavailable``    — daemon UNRESPONSIVE per heartbeat mirror;
    #                        dispatcher took the 45-31 fallback path.
    render_status: Literal["rendered", "skipped_policy", "failed", "unavailable"] = "rendered"


# ---------------------------------------------------------------------------
# NarrationEndPayload
# ---------------------------------------------------------------------------


class NarrationEndPayload(ProtocolBase):
    """Turn-completion payload, optionally carrying the final state delta.

    Port of sidequest_protocol::NarrationEndPayload.
    """

    state_delta: StateDelta | None = None
    """Optional state changes at end of narration."""

    round: int | None = None
    """The round this turn RESOLVED (pre-``record_interaction()`` bump) —
    matches the ``round`` on the turn's ACTION_REVEAL entries. Ping-pong
    2026-06-07 ("stale peer quote pinned at the bottom"): the UI anchors
    persisted peer-action quotes by round, and dice-driven turns carry no
    own PLAYER_ACTION round, so NARRATION_END is the anchor that works for
    every turn shape. ``None`` on legacy frames."""


# ---------------------------------------------------------------------------
# ThinkingPayload
# ---------------------------------------------------------------------------


class ThinkingPayload(ProtocolBase):
    """Thinking indicator (empty payload — just shows spinner).

    Port of sidequest_protocol::ThinkingPayload.
    """


# ---------------------------------------------------------------------------
# SessionEventPayload
# ---------------------------------------------------------------------------


class SessionEventPayload(ProtocolBase):
    """Session lifecycle events.

    Port of sidequest_protocol::SessionEventPayload.
    """

    event: str
    """Event type: 'connect', 'connected', 'ready', 'theme_css'."""
    player_name: str | None = None
    """Player name (on connect)."""
    genre: str | None = None
    """Genre slug (on connect)."""
    world: str | None = None
    """World slug (on connect)."""
    has_character: bool | None = None
    """Whether player has a character (on connected)."""
    initial_state: InitialState | None = None
    """Initial game state (on ready)."""
    css: str | None = None
    """Genre CSS content (on theme_css event)."""
    narrator_verbosity: NarratorVerbosity | None = None
    """Narrator verbosity setting (story 14-3). Optional for backward compat."""
    narrator_vocabulary: NarratorVocabulary | None = None
    """Narrator vocabulary/complexity setting (story 14-4). Optional for backward compat."""
    image_cooldown_seconds: int | None = None
    """Image generation cooldown in seconds (story 14-6). Optional."""
    game_slug: str | None = None
    """Slug-based game identifier (MP-01 Task 4). When set, server looks up the
    game by slug instead of the legacy genre+world+player path."""
    last_seen_seq: int = 0
    """Last event-log sequence number the client has seen (MP-03 Task 3).
    Used on reconnect so the server can replay missed events."""


# ---------------------------------------------------------------------------
# CharacterCreationPayload
# ---------------------------------------------------------------------------


class StockDeltas(ProtocolBase):
    """Mechanical deltas of one stock option (story 103-2).

    Rendered by the client BEFORE confirmation — the legible-math surface
    for mechanics-first players. ``granted_mutations`` carries DISPLAY
    NAMES (never catalog ids)."""

    attr_mods: dict[str, int] = Field(default_factory=dict)
    move: int | None = None
    ac: int | None = None
    trauma_target_mod: int = 0
    granted_mutations: list[str] = Field(default_factory=list)


class StockOption(ProtocolBase):
    """One stock on the chargen stock step (input_type 'stock', 103-2)."""

    id: str
    label: str
    description: str = ""
    deltas: StockDeltas = Field(default_factory=StockDeltas)


class CharacterCreationPayload(ProtocolBase):
    """Character creation flow payload.

    Port of sidequest_protocol::CharacterCreationPayload.
    """

    phase: str | None = None
    """Creation phase / message kind. Values:
    'scene', 'confirmation', 'complete' — server → client per-scene phase
    'back', 'edit' — legacy client → server navigation (edit being removed)
    'arrange_assign', 'arrange_clear', 'arrange_confirm', 'arrange_reject' — client → server arrangement ops
    'story_autogen', 'story_confirm' — client → server story ops
    Optional — omitted when ``action`` is set."""
    scene_index: int | None = None
    """Current scene index (1-based)."""
    total_scenes: int | None = None
    """Total number of scenes."""
    prompt: str | None = None
    """Prompt text for the player."""
    summary: str | None = None
    """Recap of previous choices."""
    message: str | None = None
    """Flavor text."""
    choices: list[CreationChoice] | None = None
    """Available choices."""
    allows_freeform: bool | None = None
    """Whether freeform text input is allowed."""
    input_type: str | None = None
    """Input type hint ('text', 'select', etc.)."""
    loading_text: str | None = None
    """Genre-aware loading text for the spinner between scenes."""
    character_preview: Any | None = None
    """Preview of the character being created."""
    rolled_stats: list[RolledStat] | None = None
    """Rolled ability scores in genre-defined order."""
    choice: str | None = None
    """Player's choice (client → server)."""
    character: Any | None = None
    """Completed character data."""
    action: str | None = None
    """Navigation action from client: 'back'."""

    # --- the stock step (server → client, story 103-2) ---
    stock_options: list[StockOption] | None = None
    """Stock options with per-stock mechanical deltas; present only when
    ``input_type`` is ``"stock"``. Aligned 1:1 with the scene's choices so
    the standard ``{phase: "scene", choice: "<index+1>"}`` response maps."""

    # --- the_arrangement (server → client) ---
    pool: list[int] | None = None
    """Six 3d6 totals waiting to be assigned to stat slots."""
    assignment: dict[str, int | None] | None = None
    """Current stat-slot assignment; None values are unfilled slots."""
    qualifying_classes: list[str] | None = None
    """Class display names that qualify for the current arrangement."""
    class_requirements: list[ClassRequirement] | None = None
    """Static panel rows (always rendered, qualified or not)."""
    confirm_enabled: bool | None = None
    """Whether the Confirm button on the_arrangement is enabled (all slots filled
    and ≥1 qualifying class)."""

    # --- the_story (server → client) ---
    pronouns_options: list[str] | None = None
    """Pronoun options offered on the_story scene."""
    pronouns_allow_freeform: bool | None = None
    """Whether the_story pronouns input accepts freeform text."""
    background_optional: bool | None = None
    """Whether the_story background field is optional."""
    description_optional: bool | None = None
    """Whether the_story description field is optional."""
    autogen_available: bool | None = None
    """Whether the autogen button is available on the_story scene."""
    autogen_result: dict[str, str] | None = None
    """When set, latest autogen output for the_story textareas."""

    # --- client → server (the_arrangement) ---
    stat: str | None = None
    """Stat slot name for arrange_assign / arrange_clear."""
    value: int | None = None
    """Pool value for arrange_assign."""

    # --- client → server (the_story) ---
    pronouns: str | None = None
    """Selected pronouns for story_confirm."""
    background: str | None = None
    """Background prose for story_confirm."""
    description: str | None = None
    """Description prose for story_confirm."""
    seed: int | None = None
    """Optional seed for story_autogen reroll-determinism."""


# ---------------------------------------------------------------------------
# TurnStatusPayload
# ---------------------------------------------------------------------------


class TurnStatusEntry(ProtocolBase):
    """One row of the sealed-letter pacing roster.

    Carried on every TURN_STATUS broadcast (see ``TurnStatusPayload.entries``)
    so every connected tab renders identical per-player rows and denominator.
    Without this, the UI accumulates entries from per-player active/submitted
    broadcasts and any dropped or late delivery diverges the counter — host
    sees "(1/2)" while peers see "(2/3)" because their accumulators contain
    different sets of broadcasts (sq-playtest 2026-05-12 [BUG-LOW] sealed-
    letter counter format differs by tab).
    """

    player_id: NonBlankString
    character_name: NonBlankString
    status: str
    """'pending' = composing, 'submitted' = sealed, 'auto_resolved' = timed out."""


class TurnStatusPayload(ProtocolBase):
    """Turn/round tracking.

    Port of sidequest_protocol::TurnStatusPayload.
    """

    player_name: NonBlankString
    """Which player this turn status is about. Non-blank."""
    status: str
    """'active' = this player's turn, 'resolved' = turn complete."""
    state_delta: StateDelta | None = None
    """Optional state delta."""
    entries: list[TurnStatusEntry] | None = None
    """Canonical sealed-letter roster snapshot — every PLAYING peer with
    current pending/submitted state. Emitted alongside every broadcast so
    each tab reconciles to the same denominator. Default-None means the
    field is absent on the wire and the UI falls back to its per-player
    accumulator (legacy path); an explicit empty list signals "no roster"
    (round resolved)."""


# ---------------------------------------------------------------------------
# ActionRevealStatus / ActionRevealPayload
# ---------------------------------------------------------------------------


class ActionRevealStatus(StrEnum):
    """Lifecycle state of a player's in-progress action visible to peers."""

    COMPOSING = "composing"
    SUBMITTED = "submitted"
    CLEARED = "cleared"


class ActionRevealPayload(ProtocolBase):
    """Per-player live action visibility update.

    See ADR-036 (Action Visibility Model). Broadcast to all party members
    except the sender so peers can coordinate during cinematic-mode rounds.
    Sealed-letter barrier and CAS dispatcher are unaffected.
    """

    player_id: NonBlankString
    """Player whose action this reveal describes."""
    character_name: NonBlankString
    """Display name of the player's character."""
    status: ActionRevealStatus
    """composing | submitted | cleared. Clients send composing/submitted; server emits cleared."""
    action: str = ""
    """Current action text. Empty string when status=cleared."""
    aside: bool = False
    """OOC aside flag, mirrors PlayerActionPayload.aside."""
    seq: int = Field(ge=0)
    """Monotonic per (player_id, round). Receivers drop non-monotonic seq within a round."""
    round: int = Field(ge=0)
    """Round counter (ADR-051). Server stamps; clients' values are overwritten."""


# ---------------------------------------------------------------------------
# AsideAnswerPayload
# ---------------------------------------------------------------------------


class AsideAnswerPayload(ProtocolBase):
    """OOC GM answer to a player aside (ADR-107).

    Never a turn record: emitting this does not advance the world, the
    turn/round counter, the narrative log, or the scrapbook. ``round`` is
    carried for client ordering only.
    """

    asker_id: str = ""
    """Player/character id that asked."""
    question: str = ""
    """Verbatim aside text the player submitted."""
    answer: str = ""
    """The GM's out-of-character reply (1-3 sentences)."""
    grounded_on: list[str] = Field(default_factory=list)
    """State keys the answer was derived from — the audit trail. Empty on
    a refusal/decline outcome."""
    round: int = 0
    """Current round at ask time. Ordering only — NOT a turn record."""


# ---------------------------------------------------------------------------
# PartyStatusPayload
# ---------------------------------------------------------------------------


class PartyStatusPayload(ProtocolBase):
    """Full party snapshot.

    Port of sidequest_protocol::PartyStatusPayload.

    ``companions`` was added in the 2026-05-06 recruitment wiring fix.
    Older clients that don't know the field render only ``members`` —
    forward-compat is fine because the payload is a strict superset.
    """

    members: list[PartyMember]
    """All party members."""
    companions: list[CompanionMember] = Field(default_factory=list)
    """Narrator-recruited NPC companions on contract with the party.
    Empty when no companions have been hired."""


# ---------------------------------------------------------------------------
# ChapterMarkerPayload
# ---------------------------------------------------------------------------


class ChapterMarkerPayload(ProtocolBase):
    """Chapter/scene marker payload.

    Port of sidequest_protocol::ChapterMarkerPayload.
    """

    title: str | None = None
    """Chapter title."""
    location: str | None = None
    """Current location name."""


# ---------------------------------------------------------------------------
# ActionQueuePayload
# ---------------------------------------------------------------------------


class ActionQueuePayload(ProtocolBase):
    """Action queue payload.

    Port of sidequest_protocol::ActionQueuePayload.
    """

    actions: list[Any] = Field(default_factory=list)
    """Queued actions."""


# ---------------------------------------------------------------------------
# ImagePayload / RenderQueuedPayload — visual-scene dispatch
# ---------------------------------------------------------------------------


class ImagePayload(ProtocolBase):
    """Rendered image reply. Mirrors the TypeScript ``ImagePayload`` in
    ``sidequest-ui/src/types/payloads.ts``. The ``url`` is served by the
    server's ``/renders/*`` static mount; absolute filesystem paths are
    translated on the way out."""

    url: str
    alt: str | None = None
    description: str | None = None
    caption: str | None = None
    render_id: str | None = None
    tier: str | None = None
    width: int | None = None
    height: int | None = None
    handout: bool | None = None


class RenderQueuedPayload(ProtocolBase):
    """Dispatch acknowledgement emitted the moment the server fires a
    render request at the daemon. The UI's ``ImageBusProvider`` uses
    ``render_id`` to hold a placeholder card until the matching
    ``IMAGE`` message lands."""

    render_id: str


# ---------------------------------------------------------------------------
# AudioCuePayload — DJ cue dispatch (mood + SFX, no daemon round-trip)
# ---------------------------------------------------------------------------


class AudioCuePayload(ProtocolBase):
    """Audio cue emitted alongside NARRATION. Tells the UI's audio provider
    which mood to crossfade to (if any) and which SFX to trigger this turn.
    Music persists across turns client-side; a turn with no mood change
    simply has ``mood=None``."""

    mood: str | None = None
    """MoodCategory.value if the interpreter detected a mood change, else None.
    None explicitly means 'no change' — the UI keeps the current track."""

    music_track: str | None = None
    """Library-relative path for the music track LibraryBackend selected.
    ``None`` when mood is None."""

    sfx_triggers: list[str] = Field(default_factory=list)
    """Zero or more SFX track paths (library-relative) to fire on this turn."""


# ---------------------------------------------------------------------------
# ErrorPayload
# ---------------------------------------------------------------------------


class ErrorPayload(ProtocolBase):
    """Error payload.

    Port of sidequest_protocol::ErrorPayload.
    """

    message: NonBlankString
    """Human-readable error message. Non-blank."""
    reconnect_required: bool | None = None
    """When true, client must re-send SESSION_EVENT{connect} before retrying."""
    code: str | None = None
    """Optional machine-readable error code so the UI can branch without
    keyword-matching the human message. Known codes:
    ``save_schema_invalid`` — saved snapshot does not match the current
    schema (e.g. legacy single-metric encounter under dual-dial migration);
    ``server_error`` — unexpected exception during message handling
    (caught at the WebSocket boundary as a safety net). Absent for
    legacy callers."""


# ---------------------------------------------------------------------------
# PlayerPresencePayload
# ---------------------------------------------------------------------------


class PlayerPresencePayload(ProtocolBase):
    """Multiplayer presence event payload (MP-02 Task 4).

    Emitted when a player connects to or disconnects from a room so other
    connected players can update their party display in real time.
    """

    player_id: str
    """The player whose connection state changed."""
    state: Literal["connected", "disconnected"]
    """Whether the player just connected or disconnected."""


# ---------------------------------------------------------------------------
# PlayerSeatPayload
# ---------------------------------------------------------------------------


class PlayerSeatPayload(ProtocolBase):
    """Player seat claim payload (MP-02 Task 5).

    Sent by a player to claim a character slot (e.g., "rux" in caverns_and_claudes).
    """

    character_slot: str
    """The character slot being claimed."""


# ---------------------------------------------------------------------------
# SeatConfirmedPayload
# ---------------------------------------------------------------------------


class SeatConfirmedPayload(ProtocolBase):
    """Seat confirmation broadcast payload (MP-02 Task 5).

    Broadcast to all players when a player claims a character slot.
    """

    player_id: str
    """The player who claimed the seat."""
    character_slot: str
    """The character slot that was claimed."""


# ---------------------------------------------------------------------------
# ConfrontationPayload
# ---------------------------------------------------------------------------


class ConfrontationPayload(ProtocolBase):
    """Payload for CONFRONTATION — drives the ConfrontationOverlay UI.

    Shape mirrors sidequest-ui/src/components/ConfrontationOverlay.tsx
    ``ConfrontationData`` (L42-58, + win_condition/player_hp/opponent_hp
    pending UI mirror). ``active=False`` signals the overlay
    to unmount. Story 3.4.

    Task 12 (2026-04-25): dual-dial migration — ``metric`` replaced by
    ``player_metric`` + ``opponent_metric``.
    """

    type: str
    label: str
    category: str
    actors: list[dict[str, Any]] = Field(default_factory=list)
    player_metric: dict[str, Any] = Field(default_factory=dict)
    opponent_metric: dict[str, Any] = Field(default_factory=dict)
    beats: list[dict[str, Any]] = Field(default_factory=list)
    secondary_stats: dict[str, Any] | None = None
    genre_slug: str
    mood: str | None = None
    active: bool = True
    # space_opera → SWN binding (Task 6): surface the resolution model and,
    # under hp_depletion, the primary combatants' HP so the player-facing
    # overlay can render the math (Sebastien/Jade legibility goal). Dial
    # packs emit ``win_condition="dial_threshold"`` and omit the hp fields,
    # which keeps the pre-existing payload shape additive. ``player_hp`` /
    # ``opponent_hp`` are ``{"current": int, "max": int}`` when present.
    # NOTE: the sidequest-ui ``ConfrontationData`` mirror needs these fields
    # added (named follow-up — UI HP track is out of scope for this task).
    win_condition: str | None = None
    player_hp: dict[str, int] | None = None
    opponent_hp: dict[str, int] | None = None
    # Story 73-4: player-facing beat-kind impact descriptor for the last beat the
    # PLAYER resolved — server-derived in beat_kinds.describe_beat_impact and
    # surfaced by build_confrontation_payload. MUST be a declared field: this
    # model is extra="forbid", so an undeclared key raises at
    # ConfrontationPayload(**payload_dict) and crashes the broadcast. Shape:
    # {"effect": str, "dial_moved": bool, "summary": str, "own": int,
    #  "opponent": int, "resolution": bool, "tag": str|None}. Absent (None) on
    # legacy payloads / before any beat — keeps the payload shape additive.
    last_beat_impact: dict[str, Any] | None = None
    # Story 73-7 — opponent-side sibling of last_beat_impact. Same serialized
    # BeatImpact shape; absent (None) when the opponent hasn't acted. Declared
    # because the model is extra="forbid".
    opponent_last_beat_impact: dict[str, Any] | None = None
    # Pingpong 2026-04-26 S2-BUG: required so ``_emit_event`` can fan out
    # CONFRONTATION frames to peer sockets (its recipient-rebuild path
    # injects the EventLog seq alongside the filtered payload). Mirrors
    # NarrationPayload.seq / SecretNotePayload.seq. Default 0 keeps
    # legacy actor-only construction sites working.
    seq: int = 0
    # SWN P4: 1d8+DEX resolution order, rolled once at instantiation. Plain list
    # for the UI (no 3D dice overlay). None/empty for rulesets with no ordering.
    initiative_order: list[dict[str, int | str]] | None = None
    # Free-for-all N-seat table (poker / auction): per-recipient projected
    # table frame. Pre-showdown: only the recipient's own seat private_state
    # (their hand / valuation) is included; other seats' private_state is
    # redacted. At showdown (resolved_winner set): all hands reveal.
    # None for every non-table confrontation.
    table_state: dict[str, Any] | None = None
    # Story 85-3 (Tier B): the session's active stakes, surfaced on the
    # CONFRONTATION channel so the promoted dockview panel can render a stakes
    # banner without racing the separate QUESTS message (Architect decision
    # 2026-06-04). ALWAYS present (None when the session has no active stakes —
    # empty string normalizes to None in build_confrontation_payload), NOT
    # additive-conditional like the hp keys. Declared because the model is
    # extra="forbid".
    stakes: str | None = None
    # Story 102-2: WWN spellcasting state for the recipient (caster economy:
    # prepared spells, casts_remaining, casts_per_day). The overlay's "Work a
    # Spell" picker needs this to render the prepared-spell list and show
    # casts_remaining (player-visible math per Sebastien/Jade legibility goal).
    # ``None`` for non-casters / B/X packs — never a fabricated empty economy.
    # ALWAYS present on the wire (like ``stakes``), so the UI gates the picker
    # on the value, not on key presence. Declared because the model is
    # extra="forbid". Shape when non-None: {"casts_remaining": int,
    # "casts_per_day": int, "prepared": list[str]} (prepared spell IDs).
    spellcasting: dict[str, Any] | None = None
    # Story 102-4: the WN sealed-round commit ledger — player-side actor names
    # whose Main Action is sealed this round. Drives the overlay's
    # committed-vs-waiting indicators (ADR-036 submit-and-wait visibility).
    # Additive-conditional: the key is set by build_confrontation_payload only
    # while a WN round has sealed commits, so native/dial frames and
    # between-round WN frames keep the legacy shape. Declared because the
    # model is extra="forbid" — an undeclared key would crash the mid-turn
    # CONFRONTATION emit on the first sealed MP commit.
    committed_actors: list[str] | None = None


# ---------------------------------------------------------------------------
# ConfrontationOutcomePayload — Phase 5 (Story 47-3)
# ---------------------------------------------------------------------------


class ConfrontationOutcomePayload(ProtocolBase):
    """Payload for CONFRONTATION_OUTCOME — drives the reveal panel.

    Shape mirrors sidequest-ui/src/components/ConfrontationOverlay.tsx
    ``ConfrontationOutcome``. Sent by the server when a magic
    confrontation resolves; the UI mounts a branch-explicit reveal
    panel above the actor portraits with the four-branch outcome
    coloring + an itemized list of mandatory_outputs (Decision #9:
    explicit panel callout at outcome time, always shown).
    """

    confrontation_id: str
    label: str
    branch: Literal["clear_win", "pyrrhic_win", "clear_loss", "refused"]
    mandatory_outputs: list[str] = Field(default_factory=list)
    # Mirrors ConfrontationPayload.seq — required for EventLog fan-out
    # to peer sockets via ``_emit_event``.
    seq: int = 0


# ---------------------------------------------------------------------------
# GamePausedPayload / GameResumedPayload
# ---------------------------------------------------------------------------


class GamePausedPayload(ProtocolBase):
    """Payload for GAME_PAUSED messages (MP-02 Task 6).

    Emitted when the room detects that one or more seated players are absent.
    The narrator will not process PLAYER_ACTION until all seated players are
    present again.
    """

    waiting_for: list[str]
    """Player IDs of seated-but-absent players causing the pause."""


# ---------------------------------------------------------------------------
# GameMessage — discriminated union over Phase 1 messages
#
# Rust wire format: {"type": "PLAYER_ACTION", "payload": {...}, "player_id": ""}
# The Rust GameMessage uses #[serde(tag = "type")] with struct variants.
# Each variant serializes "type" + its named fields ("payload", "player_id")
# as siblings in the JSON object.
#
# We model each variant as a concrete BaseModel with:
#   - type: Literal[MessageType.VARIANT] = MessageType.VARIANT (discriminator)
#   - payload: <PayloadType>
#   - player_id: str = ""
#
# GameMessage is a RootModel wrapping the Annotated union for discrimination.
# ---------------------------------------------------------------------------


class PlayerActionMessage(ProtocolBase):
    """GameMessage::PlayerAction wire representation."""

    type: Literal[MessageType.PLAYER_ACTION] = MessageType.PLAYER_ACTION
    payload: PlayerActionPayload
    player_id: str = ""


class NarrationMessage(ProtocolBase):
    """GameMessage::Narration wire representation."""

    type: Literal[MessageType.NARRATION] = MessageType.NARRATION
    payload: NarrationPayload
    player_id: str = ""


class NarrationSegmentMessage(ProtocolBase):
    """GameMessage::NarrationSegment wire representation (ADR-105 B3)."""

    type: Literal[MessageType.NARRATION_SEGMENT] = MessageType.NARRATION_SEGMENT
    payload: NarrationSegmentPayload
    player_id: str = ""


class ClientErrorMessage(ProtocolBase):
    """GameMessage::ClientError wire representation (Story 67-1).

    Inbound only — a client signalling that its render subtree crashed (a
    GameBoard ErrorBoundary catch). Because the WebSocket survives a React
    render crash, the server cannot otherwise tell a crashed client apart
    from a present-but-quiet one; this explicit signal lets the submit-and-
    wait barrier release the crashed player for the current interaction
    instead of orphaning the whole table's turn. ``player_id`` identifies
    who crashed.
    """

    type: Literal[MessageType.CLIENT_ERROR] = MessageType.CLIENT_ERROR
    payload: ClientErrorPayload
    player_id: str = ""


class PlayerSpeechMessage(ProtocolBase):
    """GameMessage::PlayerSpeech wire representation (playtest 2026-05-17).

    Verbatim PC-spoken dialogue broadcast to the whole MP party so peers
    see what each PC said. Distinct from NARRATION (narrator voice) and
    ACTION_REVEAL (ephemeral wait-phase strip, wiped on dispatch).
    """

    type: Literal[MessageType.PLAYER_SPEECH] = MessageType.PLAYER_SPEECH
    payload: SpokenLinePayload
    player_id: str = ""


class NarrationEndMessage(ProtocolBase):
    """GameMessage::NarrationEnd wire representation."""

    type: Literal[MessageType.NARRATION_END] = MessageType.NARRATION_END
    payload: NarrationEndPayload
    player_id: str = ""


class SecretNoteMessage(ProtocolBase):
    """GameMessage::SecretNote wire representation (Group G Task 6)."""

    type: Literal[MessageType.SECRET_NOTE] = MessageType.SECRET_NOTE
    payload: SecretNotePayload
    player_id: str = ""


class ScrapbookEntryMessage(ProtocolBase):
    """GameMessage::ScrapbookEntry wire representation.

    Pingpong 2026-04-26 [S3-REGRESSION]: the UI gallery merges these with
    IMAGE frames by ``turn_id``. See ``ImageBusProvider.tsx`` for the contract.
    """

    type: Literal[MessageType.SCRAPBOOK_ENTRY] = MessageType.SCRAPBOOK_ENTRY
    payload: ScrapbookEntryPayload
    player_id: str = ""


class ThinkingMessage(ProtocolBase):
    """GameMessage::Thinking wire representation."""

    type: Literal[MessageType.THINKING] = MessageType.THINKING
    payload: ThinkingPayload
    player_id: str = ""


class SessionEventMessage(ProtocolBase):
    """GameMessage::SessionEvent wire representation."""

    type: Literal[MessageType.SESSION_EVENT] = MessageType.SESSION_EVENT
    payload: SessionEventPayload
    player_id: str = ""


class CharacterCreationMessage(ProtocolBase):
    """GameMessage::CharacterCreation wire representation."""

    type: Literal[MessageType.CHARACTER_CREATION] = MessageType.CHARACTER_CREATION
    payload: CharacterCreationPayload
    player_id: str = ""


class ConfrontationMessage(ProtocolBase):
    """GameMessage::Confrontation wire representation (story 3.4)."""

    type: Literal[MessageType.CONFRONTATION] = MessageType.CONFRONTATION
    payload: ConfrontationPayload
    player_id: str = ""


class ConfrontationOutcomeMessage(ProtocolBase):
    """GameMessage::ConfrontationOutcome wire representation (Story 47-3)."""

    type: Literal[MessageType.CONFRONTATION_OUTCOME] = MessageType.CONFRONTATION_OUTCOME
    payload: ConfrontationOutcomePayload
    player_id: str = ""


class TurnStatusMessage(ProtocolBase):
    """GameMessage::TurnStatus wire representation."""

    type: Literal[MessageType.TURN_STATUS] = MessageType.TURN_STATUS
    payload: TurnStatusPayload
    player_id: str = ""


class ActionRevealMessage(ProtocolBase):
    """GameMessage::ActionReveal wire representation."""

    type: Literal[MessageType.ACTION_REVEAL] = MessageType.ACTION_REVEAL
    payload: ActionRevealPayload
    player_id: str = ""


class AsideAnswerMessage(ProtocolBase):
    """GameMessage::AsideAnswer wire representation (ADR-107).

    OOC GM reply to a player aside, broadcast table-wide. NOT a turn
    record — see :class:`AsideAnswerPayload`.
    """

    type: Literal[MessageType.ASIDE_ANSWER] = MessageType.ASIDE_ANSWER
    payload: AsideAnswerPayload
    player_id: str = ""


class PartyStatusMessage(ProtocolBase):
    """GameMessage::PartyStatus wire representation."""

    type: Literal[MessageType.PARTY_STATUS] = MessageType.PARTY_STATUS
    payload: PartyStatusPayload
    player_id: str = ""


class ChapterMarkerMessage(ProtocolBase):
    """GameMessage::ChapterMarker wire representation."""

    type: Literal[MessageType.CHAPTER_MARKER] = MessageType.CHAPTER_MARKER
    payload: ChapterMarkerPayload
    player_id: str = ""


class ActionQueueMessage(ProtocolBase):
    """GameMessage::ActionQueue wire representation."""

    type: Literal[MessageType.ACTION_QUEUE] = MessageType.ACTION_QUEUE
    payload: ActionQueuePayload
    player_id: str = ""


class ErrorMessage(ProtocolBase):
    """GameMessage::Error wire representation."""

    type: Literal[MessageType.ERROR] = MessageType.ERROR
    payload: ErrorPayload
    player_id: str = ""


class PlayerPresenceMessage(ProtocolBase):
    """GameMessage::PlayerPresence wire representation (MP-02 Task 4)."""

    type: Literal[MessageType.PLAYER_PRESENCE] = MessageType.PLAYER_PRESENCE
    payload: PlayerPresencePayload
    player_id: str = ""


class PlayerSeatMessage(ProtocolBase):
    """GameMessage::PlayerSeat wire representation (MP-02 Task 5)."""

    type: Literal[MessageType.PLAYER_SEAT] = MessageType.PLAYER_SEAT
    payload: PlayerSeatPayload
    player_id: str = ""


class SeatConfirmedMessage(ProtocolBase):
    """GameMessage::SeatConfirmed wire representation (MP-02 Task 5)."""

    type: Literal[MessageType.SEAT_CONFIRMED] = MessageType.SEAT_CONFIRMED
    payload: SeatConfirmedPayload
    player_id: str = ""


class GamePausedMessage(ProtocolBase):
    """GameMessage::GamePaused wire representation (MP-02 Task 6).

    Broadcast when one or more seated players are absent. The narrator will not
    process PLAYER_ACTION until all seated players reconnect.
    """

    type: Literal[MessageType.GAME_PAUSED] = MessageType.GAME_PAUSED
    payload: GamePausedPayload
    player_id: str = ""


class ImageMessage(ProtocolBase):
    """GameMessage::Image wire representation — carries a rendered image URL."""

    type: Literal[MessageType.IMAGE] = MessageType.IMAGE
    payload: ImagePayload
    player_id: str = ""


class RenderQueuedMessage(ProtocolBase):
    """GameMessage::RenderQueued — fires the moment the server dispatches
    a render to the daemon, before the image is ready."""

    type: Literal[MessageType.RENDER_QUEUED] = MessageType.RENDER_QUEUED
    payload: RenderQueuedPayload
    player_id: str = ""


class AudioCueMessage(ProtocolBase):
    """GameMessage::AudioCue — DJ cue shipped with NARRATION."""

    type: Literal[MessageType.AUDIO_CUE] = MessageType.AUDIO_CUE
    payload: AudioCuePayload
    player_id: str = ""


class GameResumedMessage(ProtocolBase):
    """GameMessage::GameResumed wire representation (MP-02 Task 6).

    Broadcast when the last absent seated player reconnects and the room is no
    longer paused.
    """

    type: Literal[MessageType.GAME_RESUMED] = MessageType.GAME_RESUMED
    payload: dict = {}  # noqa: RUF012 — intentionally empty payload
    player_id: str = ""


class YieldMessage(ProtocolBase):
    """GameMessage::Yield — player declares they pass their turn (no action).

    Structural intent only; no payload fields. The empty dict payload matches
    the GameResumedMessage precedent for messages with no meaningful payload.
    """

    type: Literal[MessageType.YIELD] = MessageType.YIELD
    payload: dict = {}  # noqa: RUF012 — intentionally empty payload
    player_id: str = ""


class DiceRequestMessage(ProtocolBase):
    """GameMessage::DiceRequest — server asks the rolling player to throw."""

    type: Literal[MessageType.DICE_REQUEST] = MessageType.DICE_REQUEST
    payload: DiceRequestPayload
    player_id: str = ""


class DiceThrowMessage(ProtocolBase):
    """GameMessage::DiceThrow — rolling client submits physics-settled faces."""

    type: Literal[MessageType.DICE_THROW] = MessageType.DICE_THROW
    payload: DiceThrowPayload
    player_id: str = ""


class CheckThrowPayload(ProtocolBase):
    """Client -> server: initiate a non-beat SWN skill check or save.

    ``kind="skill_check"`` resolves 2d6 via ``ruleset.check_params``;
    ``kind="save"`` resolves 1d20 via ``ruleset.save_params``.
    ``faces`` carries the physics-settled die values from the client 3D overlay
    — the server uses them via ``resolve_dice_with_faces`` (physics-is-the-roll,
    ADR-074) so the outcome matches what the player saw animate.
    """

    kind: str
    """``"skill_check"`` | ``"save"``."""
    attribute: str | None = None
    """Stat name for skill_check (e.g. ``"DEXTERITY"``). None for save."""
    save: str | None = None
    """Save category for save: ``"physical"`` | ``"evasion"`` | ``"mental"``."""
    skill_level: int = 0
    """Skill rank modifier (0–4 per SWN)."""
    difficulty_key: str | None = None
    """Difficulty ladder key for skill_check (e.g. ``"tricky"``, ``"hard"``)."""
    label: str = ""
    """Human-readable label surfaced in DiceRequest.context and OTEL span."""
    faces: list[int]
    """Physics-settled die face values from the client 3D overlay."""

    @model_validator(mode="after")
    def _require_kind_fields(self) -> CheckThrowPayload:
        if self.kind == "skill_check":
            if self.attribute is None or self.difficulty_key is None:
                raise ValueError("skill_check requires 'attribute' and 'difficulty_key'")
        elif self.kind == "save":
            if self.save is None:
                raise ValueError("save requires 'save' category")
        else:
            raise ValueError(
                f"CheckThrowPayload.kind must be 'skill_check' or 'save', got {self.kind!r}"
            )
        return self


class CheckThrowMessage(ProtocolBase):
    """GameMessage::CheckThrow — client submits faces for a non-beat SWN check/save."""

    type: Literal[MessageType.CHECK_THROW] = MessageType.CHECK_THROW
    payload: CheckThrowPayload
    player_id: str = ""


class DiceResultMessage(ProtocolBase):
    """GameMessage::DiceResult — server broadcasts resolved outcome."""

    type: Literal[MessageType.DICE_RESULT] = MessageType.DICE_RESULT
    payload: DiceResultPayload
    player_id: str = ""


class OrbitalIntentMessage(ProtocolBase):
    """GameMessage::OrbitalIntent — UI requests a chart render or scope change.

    Payload is the discriminated ``OrbitalIntent`` root model — see
    ``sidequest.protocol.orbital_intent`` for the kind-tagged union.
    """

    type: Literal[MessageType.ORBITAL_INTENT] = MessageType.ORBITAL_INTENT
    payload: OrbitalIntent
    player_id: str = ""


class OrbitalChartMessage(ProtocolBase):
    """GameMessage::OrbitalChart — server response carrying a rendered SVG."""

    type: Literal[MessageType.ORBITAL_CHART] = MessageType.ORBITAL_CHART
    payload: OrbitalIntentResponse
    player_id: str = ""


class TacticalGridMessage(ProtocolBase):
    """GameMessage::TacticalGrid — per-room cavern/settlement layout (ADR-096 Task 20b).

    Emitted by the server when the player enters a room in a world that uses
    room_graph navigation and whose room directory contains a matching YAML
    file. Carries a TacticalGridPayload; the UI Automapper routes cavern rooms
    to TacticalGridRenderer and settlement rooms to SettlementRoomView.
    """

    type: Literal[MessageType.TACTICAL_GRID] = MessageType.TACTICAL_GRID
    payload: TacticalGridPayload
    player_id: str = ""


class LocationDescriptionMessage(ProtocolBase):
    """GameMessage::LocationDescription — persistent location snapshot.

    Story 54-2 / ADR-109. Emitted on ``current_room`` change (so the UI
    Location tab updates as the party moves) and on session resume (so a
    returning client gets the manifest without polling). The delta channel
    for encounter overlays is ``LOCATION_OVERLAY_CHANGED`` (Story 54-7).
    """

    type: Literal[MessageType.LOCATION_DESCRIPTION] = MessageType.LOCATION_DESCRIPTION
    payload: LocationDescriptionPayload
    player_id: str = ""


class LocationOverlayChangedMessage(ProtocolBase):
    """GameMessage::LocationOverlayChanged — encounter overlay delta.

    Story 54-7 / ADR-109. Emitted on encounter activate (when the
    encounter has a ``location_overlay`` touching the party's current
    room) and on encounter resolve (deactivate). Payload carries the
    full post-transition overlay set for the region; on activate that's
    one overlay, on deactivate that's an empty list. The UI replaces
    its overlay slice rather than reconciling enter/leave events.
    """

    type: Literal[MessageType.LOCATION_OVERLAY_CHANGED] = MessageType.LOCATION_OVERLAY_CHANGED
    payload: LocationOverlayChangedPayload
    player_id: str = ""


class RelationshipsMessage(ProtocolBase):
    """GameMessage::Relationships — player-facing relationship roster (ADR-136).

    Emitted reactively when the relationship set changes (a disposition shift, a
    new NPC promoted into the stateful roster, or a claim recorded) — not every
    turn (Cost Scales with Drama). Global payload, broadcast to all seated PCs.
    """

    type: Literal[MessageType.RELATIONSHIPS] = MessageType.RELATIONSHIPS
    payload: RelationshipsPayload
    player_id: str = ""


class QuestsMessage(ProtocolBase):
    """GameMessage::Quests — player-facing quest spine (ADR-137 / Story 77-8).

    The RELATIONSHIPS-snapshot analog for quests. Emitted reactively when the
    spine changes (a quest minted/updated, an anchor added, stakes set) — not
    every turn (Cost Scales with Drama). Global payload, broadcast to all
    seated PCs. Transient (never event-sourced), like its LOCATION_DESCRIPTION
    / RELATIONSHIPS siblings.
    """

    type: Literal[MessageType.QUESTS] = MessageType.QUESTS
    payload: QuestsPayload
    player_id: str = ""


# ---------------------------------------------------------------------------
# CHARACTER_INCAPACITATED — a PC taken out of play (sq-playtest barsoom-3).
# ---------------------------------------------------------------------------


class CharacterIncapacitatedPayload(ProtocolBase):
    """A PC has been taken OUT of play by the genre lethality policy.

    Emitted at the moment of death (the kill turn's dispatch) and again if a
    downed seat tries to submit an action. PC-scoped via ``character_name`` so
    the UI locks only that seat's input (the rest of the band plays on — SOUL.md
    The Guitar Solo).
    """

    character_name: str
    """The downed PC's name — the UI matches this against the local character."""
    verdict: str
    """The lethality verdict: ``dead`` | ``dying`` (the LETHAL partition)."""
    status_text: str
    """The applied status, e.g. ``"Downed — dead (mortally wounded)"``."""
    headline: str
    """Player-facing death line for the banner, e.g. ``"Abinthe Moridusk has fallen."``"""
    can_reroll: bool = True
    """Whether the UI offers a new-character CTA (retention-positive death loop)."""


class CharacterIncapacitatedMessage(ProtocolBase):
    """GameMessage::CharacterIncapacitated — player-facing death surface.

    The durable input lock is server-side (``handlers.player_action`` refuses a
    downed PC's actions); this message is the UI mirror — death banner, seat
    input lock, optional re-roll CTA.
    """

    type: Literal[MessageType.CHARACTER_INCAPACITATED] = MessageType.CHARACTER_INCAPACITATED
    payload: CharacterIncapacitatedPayload
    player_id: str = ""


# ---------------------------------------------------------------------------
# DUNGEON_MAP — Beneath Sünden BETTER fix (seam 3). ADR-019 MAP_UPDATE was
# deleted in the Rust→Python port; this is the NEW ADR-055 map frame (do
# NOT revive MAP_UPDATE). Shapes mirror the UI ``MapState`` /
# ``ExploredLocation`` (sidequest-ui/src/components/MapOverlay.tsx) so the
# MapWidget's Automapper region-graph path consumes it with no adapter:
# ``id`` is the EXACT region-graph node id (the join key the narrator's
# constrained move vocabulary also uses), ``room_exits`` makes the widget
# pick the graph layout, ``is_current_room`` marks the party's region.
# ---------------------------------------------------------------------------


class DungeonMapExit(ProtocolBase):
    """One typed adjacency. ``target`` is the EXACT region-graph node id;
    ``exit_type`` is the edge kind (corridor|stairs|shaft|chute|secret)."""

    target: str
    exit_type: str


class DungeonMapLocation(ProtocolBase):
    """One discovered region, in the UI ``ExploredLocation`` shape.

    ``x``/``y`` are 0 — the procedural megadungeon has no cartesian
    coordinates (spec: keyed by region id, never by floor); the
    Automapper's layered BFS layout takes over when exit directions are
    absent. ``room_exits`` (not ``connections``) is what makes the
    MapWidget choose the graph renderer over the coordinate SVG.
    """

    id: str
    name: str
    x: float = 0.0
    y: float = 0.0
    type: str = "region"
    connections: list[str] = Field(default_factory=list)
    room_exits: list[DungeonMapExit] = Field(default_factory=list)
    room_type: str = "normal"
    is_current_room: bool = False


class DungeonMapPayload(ProtocolBase):
    """The discovered region graph projected for the UI Map tab.

    Fog-of-war: ``explored`` carries only regions in
    ``snapshot.discovered_regions`` (undiscovered neighbors stay hidden;
    an edge toward one still renders as a way that direction, exactly as
    a hand-drawn dungeon map works).
    """

    current_location: str
    region: str
    explored: list[DungeonMapLocation] = Field(default_factory=list)
    fog_bounds: dict[str, int] = Field(default_factory=lambda: {"width": 0, "height": 0})


class DungeonMapMessage(ProtocolBase):
    """GameMessage::DungeonMap — procedural megadungeon map frame.

    Emitted every narration turn of a beneath_sunden session (idempotent;
    the UI just replaces its MapState). Cures the 2026-05-17 "No map data
    yet" defect — the materialized dungeon was never projected to the UI
    after ADR-019 MAP_UPDATE was deleted in the port.
    """

    type: Literal[MessageType.DUNGEON_MAP] = MessageType.DUNGEON_MAP
    payload: DungeonMapPayload
    player_id: str = ""


# ---------------------------------------------------------------------------
# CartographyMapMessage — region-mode map data for the UI MapOverlay
# ---------------------------------------------------------------------------


class CartographyRegionWire(ProtocolBase):
    """One region in the cartography metadata for the Map tab."""

    name: str
    description: str | None = None
    adjacent: list[str] = Field(default_factory=list)


class CartographyRouteWire(ProtocolBase):
    """One route in the cartography metadata."""

    name: str
    description: str | None = None
    from_id: str | None = None
    to_id: str | None = None


class CartographyMapPayload(ProtocolBase):
    """Region-mode cartography data for the MapOverlay component.

    Mirrors the UI's MapState interface. The ``cartography`` field drives
    the region list view; ``explored`` is empty (no per-room data in
    region mode).
    """

    current_location: str
    region: str = ""
    explored: list[Any] = Field(default_factory=list)
    fog_bounds: dict[str, int] = Field(default_factory=lambda: {"width": 0, "height": 0})
    cartography: dict[str, Any] | None = None


class CartographyMapMessage(ProtocolBase):
    """Sends region-mode cartography data to the UI Map tab.

    Wire type is ``MAP_UPDATE`` — the UI App.tsx handler at
    ``msg.type === MessageType.MAP_UPDATE`` feeds it to setMapData.
    Not part of the server MessageType enum (ADR-082 deleted the old
    MAP_UPDATE pipeline; this is the minimal region-mode replacement).
    """

    type: Literal["MAP_UPDATE"] = "MAP_UPDATE"
    payload: CartographyMapPayload
    player_id: str = ""


# ---------------------------------------------------------------------------
# JOURNAL_REQUEST / JOURNAL_RESPONSE — ADR-100 Seam C (story 50-14)
# ---------------------------------------------------------------------------


class JournalRequestPayload(ProtocolBase):
    """Client asks the server for the active player's character journal.

    Empty/optional filter — the server resolves the target character via
    ``snapshot.player_seats[message.player_id]``. A player can only
    introspect their own seat (ADR-036).
    """

    filter: str | None = None


class JournalResponsePayload(ProtocolBase):
    """Server reply carrying the requesting character's known_facts."""

    entries: list[JournalEntry] = Field(default_factory=list)


class JournalRequestMessage(ProtocolBase):
    """GameMessage::JournalRequest — UI requests the player's journal replay."""

    type: Literal[MessageType.JOURNAL_REQUEST] = MessageType.JOURNAL_REQUEST
    payload: JournalRequestPayload = Field(default_factory=JournalRequestPayload)
    player_id: str = ""


class JournalResponseMessage(ProtocolBase):
    """GameMessage::JournalResponse — canonical journal replay (ADR-100 Seam C)."""

    type: Literal[MessageType.JOURNAL_RESPONSE] = MessageType.JOURNAL_RESPONSE
    payload: JournalResponsePayload
    player_id: str = ""


# Discriminated union type alias for all Phase 1 variants.
_Phase1Variant = Annotated[
    PlayerActionMessage
    | NarrationMessage
    | NarrationSegmentMessage
    | ClientErrorMessage
    | PlayerSpeechMessage
    | NarrationEndMessage
    | SecretNoteMessage
    | ScrapbookEntryMessage
    | ThinkingMessage
    | SessionEventMessage
    | CharacterCreationMessage
    | ConfrontationMessage
    | ConfrontationOutcomeMessage
    | TurnStatusMessage
    | PartyStatusMessage
    | ChapterMarkerMessage
    | ActionQueueMessage
    | ActionRevealMessage
    | AsideAnswerMessage
    | ErrorMessage
    | PlayerPresenceMessage
    | PlayerSeatMessage
    | SeatConfirmedMessage
    | GamePausedMessage
    | GameResumedMessage
    | ImageMessage
    | RenderQueuedMessage
    | AudioCueMessage
    | DiceRequestMessage
    | DiceThrowMessage
    | CheckThrowMessage
    | DiceResultMessage
    | OrbitalIntentMessage
    | OrbitalChartMessage
    | TacticalGridMessage
    | LocationDescriptionMessage
    | LocationOverlayChangedMessage
    | RelationshipsMessage
    | QuestsMessage
    | CharacterIncapacitatedMessage
    | DungeonMapMessage
    | JournalRequestMessage
    | JournalResponseMessage
    | YieldMessage,
    Field(discriminator="type"),
]


class GameMessage(RootModel[_Phase1Variant]):
    """Discriminated union of all Phase 1 GameMessage variants.

    Port of sidequest_protocol::GameMessage (#[serde(tag = "type")]).

    Wire format mirrors Rust exactly:
        {"type": "PLAYER_ACTION", "payload": {...}, "player_id": ""}

    Construct via GameMessage(root=PlayerActionMessage(...)) or parse via
    GameMessage.model_validate({...}).
    """

    @classmethod
    def parse_json(cls, data: str) -> GameMessage:
        """Parse a JSON string into a GameMessage."""
        return cls.model_validate_json(data)

    def to_json(self, **kwargs: Any) -> str:
        """Serialize to a JSON string."""
        return self.model_dump_json(**kwargs)

    @property
    def type(self) -> MessageType:
        """Return the message type discriminator."""
        return self.root.type  # type: ignore[return-value]

    @property
    def payload(self) -> Any:
        """Return the payload."""
        return self.root.payload  # type: ignore[union-attr]

    @property
    def player_id(self) -> str:
        """Return the player_id."""
        return self.root.player_id  # type: ignore[union-attr]
