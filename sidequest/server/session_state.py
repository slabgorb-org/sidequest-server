"""Leaf module for shared session state types and helpers.

Story 64-6: extracted from ``session_handler.py`` to break the import cycle
between ``session_handler`` and ``websocket_session_handler``. Both modules
import these symbols; placing them in a dependency-free leaf turns the former
import diamond into a tree.

Contains the per-connection state container (``_SessionData``), the connection
state enum (``_State``), and the stateless helpers the WebSocket handler needs
(``_hash_snapshot``, ``_shared_world_delta_to_state_delta``,
``_AUDIO_INTERPRETER``, ``_build_pc_descriptor``). ``session_handler`` re-imports
all of these so existing ``from sidequest.server.session_handler import ...``
call sites keep working.

This module must stay a leaf: it may import only modules that do not import
``session_handler`` or ``websocket_session_handler`` at runtime. ``session_helpers``
qualifies — its only ``session_handler`` import is ``TYPE_CHECKING``-guarded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from hashlib import blake2b
from typing import TYPE_CHECKING, Any

from sidequest.agents.orchestrator import Orchestrator
from sidequest.audio.interpreter import AudioInterpreter
from sidequest.audio.library_backend import LibraryBackend
from sidequest.game.builder import CharacterBuilder
from sidequest.game.history_chapter import HistoryChapter
from sidequest.game.lore_store import LoreStore
from sidequest.game.pg.dungeon import PgDungeonRepository
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.game.repository import DungeonRepository, SaveRepository, TelemetrySink
from sidequest.game.session import GameSnapshot
from sidequest.game.shared_world_delta import SharedWorldDelta
from sidequest.game.weather import WeatherState
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.scenario import ScenarioPack
from sidequest.protocol.models import PartyFormationWireEntry, StateDelta
from sidequest.server.image_pacing import ImagePacingThrottle
from sidequest.server.session_helpers import _resolve_acting_character_name

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from sidequest.dungeon.lookahead_worker import LookaheadWorkerHandle
    from sidequest.game.monster_manual import MonsterManual
    from sidequest.game.persistence import GameMode
    from sidequest.server.session_room import SessionRoom


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


# Stateless module-level AudioInterpreter; shared across all sessions.
# interpret() takes the AudioConfig as an argument, so the object
# carries no per-session state. See _maybe_dispatch_audio.
_AUDIO_INTERPRETER = AudioInterpreter()


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


def _build_pg_repos_for_slug(
    pool: ConnectionPool,
    *,
    slug: str,
    mode: str,
    genre_slug: str,
    world_slug: str,
) -> tuple[PgSaveRepository, PgDungeonRepository, PgTelemetrySink]:
    """Construct all three Postgres repositories for a session slug (ADR-115 D1).

    Called from the connect handler after the game row has been resolved.
    ``PgSaveRepository.for_slug`` is idempotent on ``session_slug`` — safe to
    call on every connect for an existing session.

    Returns a ``(repository, dungeon_repository, telemetry_sink)`` triple
    whose members all share the same ``session_id``.

    Synchronous (``ensure_session`` borrows a pooled connection).  Follows the
    existing connect-handler convention of calling blocking DB/IO helpers
    directly from the async path (e.g. ``GenreLoader.load``).
    """
    repository = PgSaveRepository.for_slug(
        pool,
        slug=slug,
        mode=mode,
        genre_slug=genre_slug,
        world_slug=world_slug,
    )
    session_id = repository.session_id
    dungeon_repository = PgDungeonRepository(pool, session_id=session_id)
    telemetry_sink = PgTelemetrySink(pool, session_id)
    return repository, dungeon_repository, telemetry_sink


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
    repository: SaveRepository  # PgSaveRepository — ADR-115 D1 (replaces SqliteStore)
    dungeon_repository: DungeonRepository  # PgDungeonRepository — ADR-115 D1
    telemetry_sink: TelemetrySink  # PgTelemetrySink — ADR-115 D1
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
    # Dogfight player-throw stash (Task 14). Set by
    # ``_apply_narration_result_to_snapshot`` when a sealed-letter cell yields
    # a player gun solution — the NPC's d20 is server-rolled and held here while
    # the player's shot awaits a client Rapier throw. Read+cleared by
    # ``DiceThrowHandler`` when the player's DICE_THROW arrives; all shots then
    # resolve together against pre-shot frame HP. None between turns (not yet
    # stashed or already consumed).
    pending_dogfight_shot: Any | None = None
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
    # Story 24-10: world-grounding state loaded once at session bootstrap
    # (sidequest.game.world_grounding_loader) and stamped onto every turn's
    # ToolContext so the get_world_grounding tool returns real data instead
    # of None. ``weather_state`` is the single per-session WeatherState
    # produced by one WeatherGenerator.generate() call at connect time (the
    # generator reads the pack-level weather.yaml); ``world_demographics`` /
    # ``world_calendar`` are the authored world-level YAML dicts loaded
    # verbatim. All three stay None for a pack/world that authored no
    # grounding (legitimate absence — get_world_grounding returns null
    # sections and the 24-6 ``tool.grounding.<section>_present=False`` attrs
    # surface, no silent fallback). Same Phase-E lifecycle as ``lore_store``
    # / ``monster_manual``: in-memory, NOT persisted to the SQLite save.
    weather_state: WeatherState | None = None
    world_demographics: dict[str, Any] | None = None
    world_calendar: dict[str, Any] | None = None
