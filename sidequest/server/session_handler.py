"""Per-connection WebSocket session lifecycle.

Port of sidequest-server/src/session.rs + the connect/playing dispatch in
dispatch/connect.rs and dispatch/mod.rs (Phase 1 narration path only).

State machine: AwaitingConnect → Creating → Playing.
- SESSION_EVENT{connect}: bind genre/world, load or create GameSnapshot, emit
  SESSION_EVENT{connected}.
- PLAYER_ACTION (in Playing state): sanitize → orchestrator → NARRATION +
  NarrationEnd + persist.
- Unsupported message in wrong state: emit ERROR, do not crash.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from hashlib import blake2b
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

if TYPE_CHECKING:
    from sidequest.dungeon.lookahead_worker import LookaheadWorkerHandle
    from sidequest.game.monster_manual import MonsterManual
    from sidequest.game.persistence import GameMode
    from sidequest.game.weather import WeatherState
    from sidequest.server.session_room import SessionRoom

from sidequest.agents.orchestrator import Orchestrator
from sidequest.audio.interpreter import AudioInterpreter
from sidequest.audio.library_backend import LibraryBackend
from sidequest.game.builder import (
    CharacterBuilder,
)
from sidequest.game.history_chapter import HistoryChapter
from sidequest.game.lore_store import LoreStore
from sidequest.game.persistence import (
    SqliteStore,
)
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection_filter import FilterDecision, ProjectionFilter
from sidequest.game.session import (
    GameSnapshot,
)
from sidequest.game.shared_world_delta import (
    SharedWorldDelta,
)
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.scenario import ScenarioPack
from sidequest.protocol.messages import (
    ConfrontationMessage,
    ConfrontationPayload,
    DungeonMapMessage,
    NarrationDelta,
    NarrationMessage,
    NarrationSegmentMessage,
    NarrationSegmentPayload,
    ScrapbookEntryMessage,
    ScrapbookEntryPayload,
    SecretNoteMessage,
    SecretNotePayload,
    TacticalGridMessage,
)
from sidequest.protocol.models import (
    PartyFormationWireEntry,
    StateDelta,
)
from sidequest.server.image_pacing import ImagePacingThrottle
from sidequest.telemetry.spans import (
    SPAN_ORCHESTRATOR_PROCESS_ACTION,  # noqa: F401 — re-exported for OTEL catalog consumers
)
from sidequest.telemetry.watcher_hub import (
    publish_event as _watcher_publish,  # noqa: F401 — back-compat re-export consumed by emitters.py
)

logger = logging.getLogger(__name__)


def _hash_snapshot(snap: object) -> str:
    """BLAKE2b-16 fingerprint of a snapshot's repr. Used for before/after change detection."""
    return blake2b(repr(snap).encode(), digest_size=16).hexdigest()


def _shared_world_delta_to_state_delta(
    delta: SharedWorldDelta,
    *,
    magic_state: dict | None = None,
) -> StateDelta:
    """Project a :class:`SharedWorldDelta` onto the wire :class:`StateDelta`.

    Story 45-1 — sealed-letter shared-world handshake. The game-side
    SharedWorldDelta is the canonical model; the protocol StateDelta is
    what the UI consumes. They share location/encounter_id/party_formation
    by intent, but the transport boundary keeps them separate so the
    game-side model can evolve without breaking wire compatibility.

    Returns a StateDelta whose perceived-state fields (characters/quests/
    items_gained) stay None — the canonical/perceived split is enforced
    structurally here, not by ad-hoc filtering.

    Magic Phase 4: ``magic_state`` is an opaque dict (already JSON-mode
    dumped from :class:`MagicState`) that rides on every NARRATION_END so
    the UI ledger panel mirrors the server registry. Pass ``None`` (the
    default) when the active world has no magic configured.
    """
    return StateDelta(
        location=delta.location or None,
        encounter_id=delta.encounter_id,
        party_formation=[
            PartyFormationWireEntry(
                player_id=entry.player_id,
                location=entry.location,
                adjacency=list(entry.adjacency),
            )
            for entry in delta.party_formation
        ]
        if delta.party_formation
        else None,
        magic_state=magic_state,
    )


tracer = trace.get_tracer("sidequest.server.session_handler")

# Stateless module-level AudioInterpreter; shared across all sessions.
# interpret() takes the AudioConfig as an argument, so the object
# carries no per-session state. See _maybe_dispatch_audio.
_AUDIO_INTERPRETER = AudioInterpreter()

# ---------------------------------------------------------------------------
# Event-kind → message class mapping (MP-03 Task 3)
# Extend this dict as additional kinds are routed through _emit_event.
# ---------------------------------------------------------------------------

_KIND_TO_MESSAGE_CLS: dict[str, type] = {
    "NARRATION": NarrationMessage,
    "NARRATION_SEGMENT": NarrationSegmentMessage,
    "CONFRONTATION": ConfrontationMessage,
    "SECRET_NOTE": SecretNoteMessage,
    "SCRAPBOOK_ENTRY": ScrapbookEntryMessage,
    # Ephemeral streaming delta — NOT event-sourced, NOT replayed on reconnect.
    # Registered here for protocol-catalog completeness only.
    "narration.delta": NarrationDelta,
    # Cavern renderer revival (ADR-096 Task 20b). Emitted on room entry; not
    # event-sourced (no replay on reconnect — room payloads are re-emitted on
    # the next room transition; the initial room is emitted at chargen time).
    "TACTICAL_GRID": TacticalGridMessage,
    # Beneath Sünden BETTER fix (seam 3). Procedural megadungeon map
    # frame; not event-sourced (re-emitted every narration turn — the UI
    # just replaces its MapState, so reconnect repopulates on the next
    # turn). The NEW ADR-055 map message (ADR-019 MAP_UPDATE is dead).
    "DUNGEON_MAP": DungeonMapMessage,
}

# Kinds persisted to the events table by side-channel writers (e.g.
# ``telemetry.watcher_hub._maybe_persist_encounter_row``) for OTEL replay
# but never fanned out to clients via ``_emit_event``. Replay must skip these
# rather than crash — pingpong 2026-04-26 [S3-BUG] Reconnect crash on
# ENCOUNTER_STARTED. Add new internal kinds here when their producers land.
_REPLAY_SKIP_KINDS: frozenset[str] = frozenset(
    {
        "ENCOUNTER_STARTED",
        "ENCOUNTER_BEAT_APPLIED",
        "ENCOUNTER_METRIC_ADVANCE",
        "ENCOUNTER_BEAT_SKIPPED",
        "ENCOUNTER_TAG_CREATED",
        "ENCOUNTER_STATUS_ADDED",
        "ENCOUNTER_YIELD",
        "ENCOUNTER_RESOLVED",
        "ENCOUNTER_RESOLUTION_SIGNAL",
    }
)


# ---------------------------------------------------------------------------
# Replay helper (MP-03 Task 4)
# Reconstructs a typed protocol message from a persisted EventRow on reconnect.
# Distinct from _emit_event (live fan-out) but reuses _KIND_TO_MESSAGE_CLS as
# the single source of truth for kind → message class mapping.
# ---------------------------------------------------------------------------

# Canonical 8-4-4-4-12 hex UUID pattern. Used to detect saves that wrote
# ``core.name`` as the opaque player UUID before the with_lobby_name fix
# landed. Anchored to reject partial matches (e.g. a UUID embedded inside
# a legitimate name like "Rux-116f74b2-...").
_UUID_HEX_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_uuid(value: str) -> bool:
    """True when ``value`` is shaped like a canonical UUID hex string.

    Case-insensitive, anchored at both ends. ``uuid.UUID(...)`` would be
    stricter (rejects weird variants), but for rename-on-resume we want
    the naive pattern match — if the saved name looks like a UUID, it's
    almost certainly the player_id that leaked in pre-fix.
    """
    return bool(_UUID_HEX_RE.match(value))


def _rename_resumed_character_if_uuid(
    *,
    snapshot: GameSnapshot,
    display_name: str,
    player_id: str,
) -> bool:
    """Rename characters whose ``core.name`` matches a UUID pattern.

    Walks ``snapshot.characters`` and, for each one whose ``core.name`` either
    equals the active ``player_id`` OR matches the canonical UUID regex,
    replaces the name with the ``display_name`` the client sent on connect.
    Returns True iff at least one character was renamed, so the caller can
    persist the snapshot. When ``display_name`` is blank or itself looks
    like a UUID we decline to rename — better to leave the UUID than to
    replace one opaque identifier with another.

    Context: pingpong 2026-04-24 — "Resumed character shows UUID as name".
    Pre-fix chargen used ``CharacterBuilder`` without ``with_lobby_name``,
    so the committed Character carried the player_id as its display name.
    The chargen path is fixed, but existing saves persist the UUID until
    the player is renamed. This function patches them on resume.

    Pydantic lets us assign through ``core.name``; the field_validator
    rejects blank strings, which is why we guard on ``display_name``.
    """
    if not display_name.strip():
        return False
    if _looks_like_uuid(display_name):
        return False
    renamed = False
    for character in snapshot.characters:
        current = character.core.name
        if current == player_id or _looks_like_uuid(current):
            character.core.name = display_name
            renamed = True
    return renamed


def _build_pc_descriptor(sd: _SessionData, pc_slug: str) -> dict | None:
    """Project the requesting socket's PC into the descriptor blob the
    daemon's ``CharacterCatalog.add_pc`` consumes.

    Returns ``None`` when the snapshot has no Character to project (e.g.
    early portraits fired before chargen confirmation, or saves that
    never seated this ``player_id``). The compose path catalog-misses on
    the ``pc:<slug>`` ref in that case and the daemon's safe wrapper falls
    back to the prose-subject prompt — so omitting the descriptor is the
    correct signal, not a silent fallback.

    Appearance prose is built from ``(race, char_class)`` only — the
    daemon replicates whatever we send to every LOD, and verbose backstory
    prose would blow the 512-token budget on the SOLO LOD. Genre packs
    that ship richer per-PC visuals can extend this later by widening
    the descriptor schema; the daemon already accepts arbitrary keys.
    """
    snapshot = sd.snapshot
    if not snapshot.characters:
        return None
    name = _resolve_acting_character_name(sd, None)
    character = next(
        (c for c in snapshot.characters if c.core.name == name),
        None,
    )
    if character is None:
        return None
    appearance = f"a {character.race} {character.char_class}".strip()
    return {
        "id": pc_slug,
        "appearance": appearance,
        "default_pose": "",
        "culture": None,
    }


def _build_message_for_kind(*, kind: str, payload_json: str, seq: int) -> object | None:
    """Build a typed protocol message from a persisted event row for replay.

    Returns ``None`` for journal-only telemetry kinds (``_REPLAY_SKIP_KINDS``)
    so replay can step over them without crashing the entire reconnect — these
    rows live in the events table for OTEL persistence (see
    ``telemetry.watcher_hub._maybe_persist_encounter_row``) but were never
    intended for the client wire. Pingpong 2026-04-26 [S3-BUG]: dropping the
    fail-loud here is intentional; the caller logs the skip via OTEL.

    Raises ValueError for kinds that are neither in the live-emit map nor on
    the explicit skip-list — that's a real schema-drift bug worth surfacing.
    """
    import json

    if kind in _REPLAY_SKIP_KINDS:
        return None

    message_cls = _KIND_TO_MESSAGE_CLS.get(kind)
    if message_cls is None:
        raise ValueError(f"_build_message_for_kind: unknown event kind {kind!r}")

    data = json.loads(payload_json)
    data["seq"] = seq

    if kind == "NARRATION":
        from sidequest.protocol.messages import NarrationPayload as _NarrationPayload

        return message_cls(payload=_NarrationPayload(**data))

    if kind == "NARRATION_SEGMENT":
        return message_cls(payload=NarrationSegmentPayload(**data))

    if kind == "CONFRONTATION":
        return message_cls(payload=ConfrontationPayload(**data))

    if kind == "SECRET_NOTE":
        return message_cls(payload=SecretNotePayload(**data))

    if kind == "SCRAPBOOK_ENTRY":
        return message_cls(payload=ScrapbookEntryPayload(**data))

    # Unreachable: _KIND_TO_MESSAGE_CLS guard above catches unknowns.
    # Kept as a belt-and-suspenders hard fail.
    raise ValueError(f"_build_message_for_kind: no payload constructor for kind {kind!r}")


# ---------------------------------------------------------------------------
# Per-turn write-split: canonical save + per-peer filtered frames (G8)
# ---------------------------------------------------------------------------
#
# MP spec 2026-04-22: the canonical save on the narrator-host holds the union
# of every event as appended to EventLog (unfiltered). Each peer save holds
# only the per-peer filtered subset — the frames whose FilterDecision.include
# is True.
#
# `_project_frames` is the single shared core: given one envelope + filter +
# list of connected players, compute the per-recipient decisions. Both the
# production turn driver (`_emit_event`) and the test-facing helper
# `apply_turn_writes_for_test` route through this function so the invariant
# is tested in the same code path production exercises.


@dataclass
class SentFrame:
    """One outbound frame to one peer after projection filter."""

    player_id: str
    payload_json: str


def _project_frames(
    *,
    envelope: MessageEnvelope,
    projection_filter: ProjectionFilter,
    connected_players: list[str],
    view: object = None,
    on_decision: Callable[[str, FilterDecision], None] | None = None,
) -> list[tuple[str, FilterDecision]]:
    """Run the projection filter once per connected player.

    Returns every (player_id, decision) pair — caller decides what to do with
    excluded decisions (e.g. production still writes them to the projection
    cache via ``on_decision`` before discarding the frame).

    The canonical EventLog append is the caller's responsibility; this helper
    is purely the filter fan-out step.
    """
    decisions: list[tuple[str, FilterDecision]] = []
    for pid in connected_players:
        decision = projection_filter.project(envelope=envelope, view=view, player_id=pid)
        if on_decision is not None:
            on_decision(pid, decision)
        decisions.append((pid, decision))
    return decisions


def apply_turn_writes_for_test(
    *,
    event_log: object,
    filter: ProjectionFilter,
    envelope: dict,
    connected_players: list[str],
    view: object = None,
) -> list[SentFrame]:
    """Test-facing write-split helper: canonical append + per-peer filter.

    Exercises the same core (`_project_frames`) as the production turn driver.
    The test fake ``event_log`` accepts a single positional MessageEnvelope on
    ``append``; production uses ``append_in_transaction(kind=..., payload_json=...)``
    inside a DB transaction — both converge on `_project_frames` for the
    per-peer decision loop.

    Canonical save receives the raw envelope exactly once. Each peer frame is
    emitted only when ``FilterDecision.include`` is True.
    """
    import json as _json

    canonical_env = MessageEnvelope(
        kind=envelope["kind"],
        payload_json=_json.dumps(envelope["payload"]),
        origin_seq=getattr(event_log, "next_seq", 0),
    )
    event_log.append(canonical_env)  # type: ignore[attr-defined]

    decisions = _project_frames(
        envelope=canonical_env,
        projection_filter=filter,
        connected_players=connected_players,
        view=view,
    )
    return [
        SentFrame(player_id=pid, payload_json=decision.payload_json)
        for pid, decision in decisions
        if decision.include
    ]


# ---------------------------------------------------------------------------
# Session state machine
# ---------------------------------------------------------------------------


class _State(Enum):
    AwaitingConnect = auto()
    Creating = auto()
    Playing = auto()


@dataclass
class _SessionData:
    """Mutable session state once genre/world are bound."""

    genre_slug: str
    world_slug: str
    player_name: str
    player_id: str
    snapshot: GameSnapshot
    store: SqliteStore
    genre_pack: GenrePack
    orchestrator: Orchestrator
    # Back-reference to the per-slug SessionRoom. Populated by the connect
    # handler at construction time so any function with `sd` in scope can
    # reach `sd._room.session`. Optional only because pre-slug-connect
    # paths construct _SessionData without a room — the slug-connect path
    # always sets this. Placed here (after the last non-default field) to
    # satisfy dataclass field-ordering rules; the plan's "next to store"
    # placement breaks ordering since genre_pack/orchestrator have no
    # defaults.
    _room: SessionRoom | None = None
    # Character builder is present only during the Creating state. Initialized
    # in _handle_connect when has_character=False; consumed (and discarded) by
    # _handle_character_creation's confirmation commit when the Character lands
    # on snapshot.characters.
    builder: CharacterBuilder | None = None
    # Opening-hook seed + directive (Story 2.3 Slice B). Resolved once at
    # connect time from pack/world.openings. Both consumed together by the
    # opening-turn bootstrap after chargen confirmation (Slice H): the seed
    # becomes the first player action, the directive is injected into the
    # narrator's Early zone on turn 0 only. ``None`` means the pack has no
    # opening-hook entries — the first turn runs without a directive.
    opening_seed: str | None = None
    opening_directive: str | None = None
    # Canned-openings Phase 4 (Task 19): id of the Opening picked at
    # chargen-completion. Read by ``record_opening_played`` at directive
    # consumption so the ``opening.played`` span carries opening_id for
    # GM-panel attribution. None until ``_populate_opening_directive_on_
    # chargen_complete`` resolves an opening.
    _resolved_opening_id: str | None = None
    # Narrator world context (Story 41-11 — closes the Phase 2.2 IOU
    # ``Culture.chargen`` filter). Resolved once at connect time from
    # pack/world.cultures with lore-only cultures filtered out; injected
    # into the narrator prompt's Valley zone on every turn. ``None`` when
    # the pack declares no cultures (empty reference string also
    # normalised to ``None`` so the zone section is skipped cleanly).
    # Phase 3 will extend this field to include setting + world-lore
    # blocks alongside the culture reference.
    world_context: str | None = None
    # Lore store (Story 2.3 Slice F). Per-session indexed knowledge
    # collection. Seeded at chargen confirmation from the builder's
    # scene choices so the narrator's RAG retrieval pipeline can see
    # the player's backstory decisions. Rust parity: Arc<Mutex<LoreStore>>
    # on app state — Python single-player keeps it on the session.
    lore_store: LoreStore = field(default_factory=LoreStore)
    # Audio DJ — per-session LibraryBackend so ThemeRotator cooldowns
    # persist across turns within a session. None when the genre pack
    # has no resolvable audio directory on disk (e.g. a pack defining
    # moods without a matching ``audio/`` subtree). See
    # _maybe_dispatch_audio for the dispatch path.
    audio_backend: LibraryBackend | None = None
    # Active scenario pack (Story 2.3 Slice D). Set at chargen
    # confirmation when the genre pack declares at least one scenario.
    # Rust parity: ``shared_session.active_scenario`` — lives on the
    # shared session in Rust's multi-player model; Python Phase 1 is
    # single-player so it lands on the connection-scoped state. Later
    # slices consume this for pressure events, scene-budget gating,
    # and accusation UI.
    active_scenario: ScenarioPack | None = None
    # MP-01 Task 4: slug-based connect fields. Set when connecting via
    # game_slug rather than the legacy genre+world path.
    game_slug: str | None = None
    mode: GameMode | None = None
    # Lore embed worker lifecycle (Story 37-33 round-trip #4). A live
    # reference to the most recent background embed task so cleanup() can
    # cancel it before the SQLite store closes and so _dispatch_embed_worker
    # can skip dispatch while a previous worker is still running. Both
    # guards prevent the fire-and-forget task from writing to an orphaned
    # in-memory lore_store after disconnect and from racing a sibling worker
    # at the ``await client.embed()`` yield point on rapid successive turns.
    embed_task: asyncio.Task[None] | None = None
    # Beneath Sünden Plan 7 (§8): live dungeon look-ahead worker handle.
    # Set by the connect handler via attach_dungeon_to_session for
    # beneath_sunden sessions; None for every other genre/world and for
    # sessions that disconnect before attach. cleanup() drains it via
    # detach_dungeon_from_session before the final save. TYPE_CHECKING
    # import + `from __future__ import annotations` (top of file) keep
    # this lazy — no runtime import of the dungeon package is needed.
    lookahead_handle: LookaheadWorkerHandle | None = None
    # Last dice roll outcome (story 34 — physics-is-the-roll). Stashed on
    # DICE_THROW resolution and read by the next narration turn's context
    # builder so the narrator knows whether the roll succeeded. Cleared by
    # the consuming turn (``take`` semantics). None when no roll is
    # pending. Rust parity: ``pending_roll_outcome`` on SharedSession.
    pending_roll_outcome: Any | None = None
    # Rolling character's name for the dice-replay turn — paired with
    # ``pending_roll_outcome``. Read by ``_apply_narration_result_to_snapshot``
    # so it can drop ONLY the rolling actor's beat from the narrator's
    # ``beat_selections`` (already applied via ``dispatch_dice_throw``)
    # while still applying opponent-side beat selections so the opponent
    # dial can advance. Playtest 2026-04-25 [P0] regression: dropping all
    # selections wholesale left the opponent dial inert and made combat
    # one-sided. Cleared together with ``pending_roll_outcome``.
    pending_roll_actor: str | None = None
    # Opposed-check pending state (combat fairness, 2026-04-26). Set by
    # ``dispatch_dice_throw`` when the active confrontation declares
    # ``resolution_mode: opposed_check`` — the player's beat is NOT yet
    # applied (waiting for the narrator to pick the opponent's beat so
    # the resolver can derive the tier). Read by
    # ``_apply_narration_result_to_snapshot`` which rolls the opponent's
    # d20, runs ``resolve_opposed_check``, emits the lie-detector OTEL
    # span, and applies both beats. Cleared by the consuming turn.
    pending_opposed_player_d20: int | None = None
    pending_opposed_player_beat_id: str | None = None
    # ADR-050 — image pacing throttle. Per-session, time-based cooldown that
    # suppresses render dispatches faster than human absorption speed.
    # Default 30s solo / 60s MP; created at chargen confirmation once the
    # session ``mode`` is known. Defaults to a solo throttle so the field
    # is always non-None for legacy/test session-data construction sites
    # that don't set ``mode`` explicitly.
    # NOTE: per-process state. Multi-worker uvicorn would split the throttle
    # across workers; revisit with a shared backing store if we go there.
    image_pacing_throttle: ImagePacingThrottle = field(default_factory=ImagePacingThrottle.for_solo)
    # Story 45-31: in-flight render counter for the backpressure check.
    # Incremented at enqueue time; decremented in the background render
    # task on completion or failure. The dispatcher reads this before
    # accepting a new render so the warn fires when concurrent depth
    # exceeds ``render_backpressure_threshold``.
    render_in_flight: int = 0
    # Story 45-31: per-session diagnostic counters used by the
    # post-session render diagnostic writer. Updated by the dispatcher
    # alongside the watcher events so the JSON snapshot captures the
    # session's render lifetime without re-walking the watcher stream.
    render_enqueue_count: int = 0
    render_backpressure_warn_count: int = 0
    render_unresponsive_window_count: int = 0
    last_successful_render_id: str | None = None
    last_successful_render_ts_iso: str | None = None
    # Story 45-31: set in the turn pipeline immediately before the
    # scrapbook emit when the daemon-state mirror reports UNRESPONSIVE.
    # The dispatcher reads this in ``_maybe_dispatch_render`` to skip
    # the daemon round-trip (the scrapbook row already carries
    # ``render_status="unavailable"``, no second persist needed).
    render_unavailable_pending: bool = False
    # Monster Manual (ADR-059 port). Persistent pre-generated NPC and
    # encounter pool keyed by (genre, world). Lazy-loaded on the first
    # narration turn by ``monster_manual_inject.ensure_loaded`` —
    # ``None`` before that and for any session whose genre never bound.
    # Saved to disk after each turn so activations / dormancy persist
    # across reconnects.  Rust parity: ``DispatchContext.monster_manual``
    # was a per-dispatch reference reloaded from disk on every turn;
    # Python keeps the same Manual instance for the session's lifetime
    # and saves at turn end (fewer JSON parses, identical on-disk state).
    monster_manual: MonsterManual | None = None
    # Story 45-19: parsed history chapters cached at chargen so the
    # arc-recompute tick doesn't re-parse history.yaml on every turn.
    # Populated alongside the chargen-time materialization in
    # ``_handle_character_creation`` (websocket_session_handler.py); the
    # post-``record_interaction`` recompute call in
    # ``_execute_narration_turn`` reads it. Default empty list so
    # sessions whose pack ships no history still construct cleanly —
    # the recompute helper is a graceful no-op on an empty chapter list.
    cached_history_chapters: list[HistoryChapter] = field(default_factory=list)
    # Story 24-10: world-grounding carrier fields, loaded once at connect
    # via ``load_world_grounding`` and stamped here (mirroring lore_store /
    # monster_manual). ``_build_turn_context`` copies them onto every
    # per-turn TurnContext so the get_world_grounding tool returns real data.
    # None for packs/worlds that authored no weather/demographics/calendar —
    # the graceful path keeps the tool returning null sections, no crash.
    weather_state: WeatherState | None = None
    world_demographics: dict[str, Any] | None = None
    world_calendar: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Module-level helpers — extracted to session_helpers.py and narration_apply.py.
# Re-exported here so existing imports (tests, external callers) keep working.
# ---------------------------------------------------------------------------

from sidequest.server.narration_apply import (  # noqa: E402 — back-compat re-export
    _apply_narration_result_to_snapshot,
)
from sidequest.server.session_helpers import (  # noqa: E402 — back-compat re-export
    _build_turn_context,
    _detect_missed_recurring_npcs,
    _detect_npc_identity_drift,
    _error_msg,
    _find_confrontation_def,
    _presence_msg,
    _render_url_from_path,
    _resolve_acting_character_name,
    _resolve_location_display,
    _sfx_ids_from_genre,
    _world_history_value,
    aggregate_visibility,
    build_secret_note_events,
    emit_secret_notes,
)
from sidequest.server.websocket_session_handler import (  # noqa: E402 — back-compat re-export
    WebSocketSessionHandler,
    _populate_opening_directive_on_chargen_complete,
)

__all__ = [
    # Top-level types defined in this module
    "SentFrame",
    "WebSocketSessionHandler",
    "_SessionData",
    "_State",
    # Module-level helpers re-exported from session_helpers / narration_apply
    "_apply_narration_result_to_snapshot",
    "_build_turn_context",
    "_detect_missed_recurring_npcs",
    "_detect_npc_identity_drift",
    "_error_msg",
    "_find_confrontation_def",
    "_populate_opening_directive_on_chargen_complete",
    "_presence_msg",
    "_render_url_from_path",
    "_resolve_acting_character_name",
    "_resolve_location_display",
    "_sfx_ids_from_genre",
    "_world_history_value",
    "aggregate_visibility",
    "apply_turn_writes_for_test",
    "build_secret_note_events",
    "emit_secret_notes",
]
