"""ConnectHandler — handles the connect sub-event of SESSION_EVENT.

Called from :class:`~sidequest.handlers.session_event.SessionEventHandler`
when ``payload.event == "connect"``.  The signature mirrors the original
``_handle_connect(payload, player_id)`` — not the generic ``(session, msg)``
Protocol — because the caller already unpacks those fields.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import TYPE_CHECKING

from sidequest.agents.orchestrator import Orchestrator
from sidequest.game.builder import CharacterBuilder
from sidequest.game.event_log import EventLog
from sidequest.game.lore_seeding import seed_world_lore
from sidequest.game.persistence import (
    SaveSchemaIncompatibleError,
    SqliteStore,
)
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.scrapbook_coverage import detect_scrapbook_coverage_gaps
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import GenreLoader
from sidequest.protocol.messages import (
    ChapterMarkerMessage,
    ChapterMarkerPayload,
    ConfrontationMessage,
    ConfrontationPayload,
    Footnote,
    GameResumedMessage,
    NarrationMessage,
    SeatConfirmedMessage,
    SeatConfirmedPayload,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server import views
from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes
from sidequest.server.dispatch.culture_context import resolve_culture_reference
from sidequest.server.image_pacing import ImagePacingThrottle
from sidequest.server.magic_init import init_magic_state_for_session
from sidequest.server.session_handler import (
    _build_message_for_kind,
    _rename_resumed_character_if_uuid,
    _SessionData,
    _State,
)
from sidequest.server.session_helpers import (
    _error_msg,
    _presence_msg,
    _resolve_location_display,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from collections.abc import Callable

    from sidequest.game.lore_store import LoreStore
    from sidequest.genre.models.pack import GenrePack
    from sidequest.protocol.messages import SessionEventPayload
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

from opentelemetry import trace

logger = logging.getLogger(__name__)

# Story 24-10 / 24-8 playtest: hardcoded weather sample point for the
# tea_and_murder/glenross session. The generator is deterministic — these
# pin the (zone, season, seed) the bootstrap samples once at connect.
# Calendar-driven season advancement and per-zone selection are follow-up
# stories; kept here (not buried in the loader) so the playtest's chosen
# point is visible at the call site.
_GROUNDING_WEATHER_ZONE = "glen_floor"
_GROUNDING_WEATHER_SEASON = "autumn"
_GROUNDING_WEATHER_SEED = 1908


def _mint_replay_footnote_fact_ids(msg: object) -> tuple[object, int]:
    """ADR-100 Seam C, replay/backfill path.

    Pre-fix saves persisted NARRATION footnotes with ``fact_id=None``. The
    client (``useStateMirror.ts``) drops any footnote lacking a ``fact_id``
    by design, so every resume of an old save silently loses those
    KnownFacts (sq-playtest 2026-05-16: the 5 Agna-ledger scene facts gone
    on every rehydration). Carried fix #4 added a defensive mint to the
    *live* footnote-forwarding loop only; the replay/tail-backfill path
    replays persisted footnotes verbatim and never runs them through it.

    Mirror the live mint here. The digest MUST stay byte-identical to the
    canonical algorithm in
    ``server/websocket_session_handler.py`` (search ``digest_input =``,
    ~2943): a replayed pre-fix fact and its later live re-narration have to
    collide on the same id so the client's ``seenFactIds`` dedupe — and the
    journal — treat them as one fact. Narrator-supplied ``fact_id``s are
    preserved untouched (scenario clue_intake Seam A matches them against
    genre-authored ``ClueNode.id``). Non-NARRATION messages and footnote-
    free narrations are returned unchanged.

    Returns ``(msg_or_rebuilt_copy, count_of_ids_minted)``.

    Only NARRATION carries footnotes, and all three replay emission sites
    (cached-rows loop, legacy live-filter fallback, and the tail-backfill
    via ``views.backfill_last_narration_block``) construct their messages
    through the one ``_build_message_for_kind`` factory, so an
    ``isinstance(NarrationMessage)`` guard is exact at every site — and
    gives the type checker full narrowing instead of stringly ``getattr``.

    NOTE (forward / Sprint-3 cleanup): the digest lives in two places now
    (live loop + this helper). A shared ``mint_footnote_fact_id(footnote)``
    consumed by both paths is the drift-proof design — deferred here to
    avoid editing the not-yet-fully-verified carried-#4 live path mid-
    playtest. If you touch either copy, change BOTH or dedupe breaks.
    """
    if not isinstance(msg, NarrationMessage):
        return msg, 0
    footnotes = msg.payload.footnotes
    if not footnotes:
        return msg, 0
    minted = 0
    rebuilt: list[Footnote] = []
    for fn in footnotes:
        if fn.fact_id is None:
            cat_str = (
                fn.category.value if hasattr(fn.category, "value") else str(fn.category)
            )
            digest_input = f"{fn.summary}|{cat_str}|{fn.is_new}"
            digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
            rebuilt.append(fn.model_copy(update={"fact_id": f"fn-{digest[:16]}"}))
            minted += 1
        else:
            rebuilt.append(fn)
    if minted == 0:
        return msg, 0
    return (
        msg.model_copy(
            update={"payload": msg.payload.model_copy(update={"footnotes": rebuilt})}
        ),
        minted,
    )


def _backfill_magic_state_on_resume(
    *,
    snapshot: GameSnapshot,
    genre_pack,
    world_slug: str,
) -> None:
    """Backfill ``snapshot.magic_state`` on resume if absent and the
    world ships ``magic.yaml``.

    Pre-fix (playtest 2026-04-30 #9), saves created before
    ``init_magic_state_for_session`` was wired into chargen — or via any
    code path that skipped the chargen branch — landed on resume with
    ``snapshot.magic_state = None``. The ``magic_working`` pipeline then
    silently no-op'd: server-side validation ran but had no
    character-keyed bars to debit, so threshold-promotion → status
    updates never landed and the LedgerPanel never surfaced bars. The
    failure was perfectly silent — exactly the kind of half-wired feature
    CLAUDE.md "Verify Wiring, Not Just Existence" forbids.

    The helper is a no-op when:
      - the world has no ``magic.yaml`` pair (correct: non-magic worlds
        keep ``magic_state=None``);
      - ``snapshot.magic_state`` is already populated (resume from a
        post-init save);
      - the snapshot has no ``characters`` to key the ledger by;
      - ``init_magic_state_for_session`` returns ``False`` (loader
        error — already logged loud at ERROR by the helper).

    Emits a ``magic_state.backfilled_on_resume`` OTEL span on success so
    the GM panel can spot when a backfill actually fired (per CLAUDE.md
    OTEL Observability Principle: subsystem fixes must be GM-panel-
    visible).
    """
    if snapshot.magic_state is not None:
        return
    if not snapshot.characters:
        # No PC seated yet — nothing to add to the ledger. The
        # chargen-bootstrap path will call init_magic_state_for_session
        # itself once a character is materialized.
        return
    source_dir = getattr(genre_pack, "source_dir", None)
    if source_dir is None:
        return
    seated = snapshot.characters[0]
    character_id = seated.core.name
    character_class = seated.char_class
    tracer = trace.get_tracer("sidequest.handlers.connect")
    with tracer.start_as_current_span("magic_state.backfill_on_resume") as span:
        span.set_attribute("world_slug", world_slug)
        span.set_attribute("character_id", character_id)
        span.set_attribute("character_class", character_class or "")
        loaded = init_magic_state_for_session(
            snapshot=snapshot,
            genre_pack_source_dir=source_dir,
            world_slug=world_slug,
            character_id=character_id,
            character_class=character_class,
        )
        span.set_attribute("loaded", loaded)
        if loaded:
            logger.info(
                "magic.backfilled_on_resume world=%s character=%s",
                world_slug,
                character_id,
            )


def _seed_world_lore_on_resume(
    *,
    lore_store: LoreStore,
    genre_pack: GenrePack,
    world_slug: str | None,
    emit: Callable[..., None],
) -> tuple[int, int]:
    """Re-seed the per-session in-memory ``LoreStore`` with the
    deterministic genre + world lore on the slug-resume connect path.

    Root cause (exposed by commit 72750db once ToolContext id/store
    plumbing was fixed): ``_SessionData.lore_store`` is an in-memory
    ``field(default_factory=LoreStore)`` and is NOT persisted to the
    SQLite save. Only the fresh character-creation flow
    (``_chargen_confirmation``) seeded it. On a slug-resume connect a
    brand-new ``_SessionData`` is constructed with an *empty* lore_store
    and ``_chargen_confirmation`` is never reached — so ``query_lore``
    returned ``hit_count=0`` and the SDK narrator confabulated world
    canon instead of recalling it. Every resumed save was affected.

    Delegates to the shared :func:`seed_world_lore` so the resume path
    and the fresh path share one implementation (DRY / "wire up what
    exists"). Idempotent: stable fragment ids + the ``DuplicateLoreId``
    guard mean a reconnect-of-a-reconnect adds zero and never grows the
    store unboundedly.

    Scope: genre + world lore only. Char-creation lore is deliberately
    NOT re-seeded here — the chargen ``builder.scenes()`` is not
    reliably available on a pure resume; that is a separate concern.

    ``emit`` is the ``lore_store_loaded`` watcher callback so the GM
    panel / Jaeger can prove lore loaded on resume (CLAUDE.md OTEL
    lie-detector mandate) — the SAME event the fresh path emits.
    """
    return seed_world_lore(lore_store, genre_pack, world_slug, emit=emit)


class ConnectHandler:
    """Handle the ``connect`` sub-event of SESSION_EVENT.

    Restores or creates a game session, initialises chargen when needed,
    replays missed events for reconnecting players, and returns the
    bootstrap message list (connected + chargen scene or ready + replay).
    """

    async def handle(
        self,
        session: WebSocketSessionHandler,
        payload: SessionEventPayload,
        player_id: str,
    ) -> list[object]:
        # Slug-keyed connect is the only supported path (Story 45-26).
        # Falsy game_slug returns a typed error below.
        if getattr(payload, "game_slug", None):
            from sidequest.game.persistence import (
                GameMode,
                db_path_for_slug,
                get_game,
            )

            slug = payload.game_slug
            db = db_path_for_slug(session._save_dir, slug)
            if not db.exists():
                return [_error_msg(f"unknown game slug: {slug}")]
            store = SqliteStore(db)
            store.initialize()
            from sidequest.telemetry.watcher_hub import bind_event_store as _bind_event_store

            _bind_event_store(store)
            row = get_game(store, slug)
            if row is None:
                return [_error_msg(f"unknown game slug: {slug}")]
            if not player_id:
                player_id = str(uuid.uuid4())

            # Display name (playtest 2026-04-23 Bug 1). The UI stores the
            # player's chosen display name in localStorage and sends it on
            # the slug-connect envelope as ``payload.player_name``. Without
            # it the character-name fallback (``CharacterBuilder.with_
            # lobby_name``) falls back to the opaque player UUID — so
            # genre packs with no name-entry scene (mutant_wasteland etc.)
            # end up with a UUID string on the character sheet header.
            #
            # No silent fallback: if the client didn't send a name, log
            # loud and use the player_id as a last resort. The UI should
            # always send this field on the slug-connect payload — a
            # missing value is a protocol contract violation, not a
            # normal operating mode.
            display_name: str = (payload.player_name or "").strip()
            if not display_name:
                logger.warning(
                    "session.slug_connect.missing_player_name slug=%s player_id=%s "
                    "— falling back to player_id as display name. UI must send "
                    "payload.player_name on slug-connect envelopes.",
                    slug,
                    player_id,
                )
                display_name = player_id

            # Room registry wiring (MP-02 Task 2). attach_room_context must
            # have been called — slug-connect cannot proceed without a room
            # registry, socket id, and outbound queue. Fail loudly if the
            # WebSocket lifecycle was bypassed (no silent test-only path).
            if (
                session._room_registry is None
                or session._socket_id is None
                or session._out_queue is None
            ):
                raise RuntimeError(
                    "slug-connect requires attach_room_context() to have been "
                    "called first — WebSocket lifecycle wiring is missing. "
                    "Production: ws_endpoint calls it immediately after accept(). "
                    "Tests: construct a RoomRegistry and call attach_room_context."
                )
            from sidequest.server.session_room import SoloSlotConflict
            from sidequest.telemetry.spans import mp_slug_connect_span

            with mp_slug_connect_span(
                slug=slug,
                player_id=player_id,
                mode=str(row.mode.value) if hasattr(row.mode, "value") else str(row.mode),
            ) as _mp_span:
                room = session._room_registry.get_or_create(slug, mode=GameMode(row.mode))
                # Snapshot pause state BEFORE connecting so we can detect
                # whether this connect resolved an existing pause (MP-02 Task 6).
                was_paused_before_connect = room.is_paused()
                # Snapshot the peer set BEFORE we add the new player to
                # ``_connected``. This is the back-fill source for the
                # PLAYER_PRESENCE roster the new client needs (playtest
                # 2026-04-26 S2-BUG): the server already broadcasts
                # PLAYER_PRESENCE on each new connect (so later joiners show
                # up live for earlier joiners), but never sent the *new*
                # connection a snapshot of who was already there. Net effect
                # in a 4-player game: P1 sees all 4, P2 sees 3, P3 sees 2,
                # P4 sees only self. Excludes the connecting player_id in
                # case this is a same-player reconnect on a new socket.
                peers_to_backfill = [pid for pid in room.connected_player_ids() if pid != player_id]
                try:
                    room.connect(player_id, socket_id=session._socket_id)
                except SoloSlotConflict as exc:
                    _mp_span.set_attribute("solo_slot_conflict", True)
                    return [_error_msg(str(exc))]
                session._room = room
                room.attach_outbound(session._socket_id, session._out_queue)
                room.broadcast(
                    _presence_msg(player_id, "connected"),
                    exclude_socket_id=session._socket_id,
                )
                # PLAYER_PRESENCE back-fill (playtest 2026-04-26 S2-BUG).
                # Push one PRESENCE{connected} frame per pre-existing peer
                # into THIS socket's outbound queue. Existing peers' rosters
                # are already correct (the standard outbound broadcast above
                # handles them); only the connecting client needs catch-up.
                # Mirrors the live-join shape: the UI's PLAYER_PRESENCE
                # handler folds these straight into ``connectedPeerIds`` —
                # no UI change required (per OQ-2 analysis).
                for peer_id in peers_to_backfill:
                    session._out_queue.put_nowait(_presence_msg(peer_id, "connected"))
                if peers_to_backfill:
                    _watcher_publish(
                        "session_presence_backfill",
                        {
                            "slug": slug,
                            "player_id": player_id,
                            "backfilled_count": len(peers_to_backfill),
                            "backfilled_player_ids": peers_to_backfill,
                        },
                        component="session",
                        severity="info",
                    )
                    logger.info(
                        "session.presence_backfill slug=%s player_id=%s "
                        "backfilled_count=%d backfilled_player_ids=%s",
                        slug,
                        player_id,
                        len(peers_to_backfill),
                        peers_to_backfill,
                    )
                _mp_span.set_attribute("presence_backfill_count", len(peers_to_backfill))
                # If this connect resolved the pause (was paused before, not
                # paused now), broadcast GAME_RESUMED to all players
                # including the reconnecting socket (MP-02 Task 6).
                resolved_pause = was_paused_before_connect and not room.is_paused()
                if resolved_pause:
                    room.broadcast(
                        GameResumedMessage(),
                        exclude_socket_id=None,
                    )
                _mp_span.set_attribute("was_paused_before", was_paused_before_connect)
                _mp_span.set_attribute("resolved_pause", resolved_pause)
                _mp_span.set_attribute("connected_count", len(room.connected_player_ids()))

            # Load genre pack (Bug 1 fix: genre_pack must not be None).
            try:
                loader = GenreLoader(search_paths=session._search_paths)
                genre_pack = loader.load(row.genre_slug)
                # World directory used by orbital-tier loader at room
                # bind time. ``loader.find`` raises if the pack is gone,
                # but the ``loader.load`` above already validated it.
                pack_dir = loader.find(row.genre_slug)
                world_dir = pack_dir / "worlds" / row.world_slug
            except Exception as exc:
                logger.error(
                    "session.genre_load_failed genre=%s slug=%s error=%s",
                    row.genre_slug,
                    slug,
                    exc,
                )
                return [_error_msg(f"Failed to load genre pack '{row.genre_slug}': {exc}")]

            # Restore saved snapshot, or start fresh (Bug 2 fix: resume semantics).
            try:
                saved = store.load()
            except SaveSchemaIncompatibleError as exc:
                # Schema-incompatible save (e.g. legacy single-metric encounter
                # under dual-dial migration). Don't let pydantic's
                # ValidationError bubble up to websocket.py — that path closes
                # the socket without surfacing a reason and the UI sits in an
                # infinite reconnect loop. Return a typed ERROR so the UI can
                # render an actual error panel with escape actions.
                logger.warning(
                    "session.save_schema_invalid slug=%s path=%s error=%s",
                    slug,
                    exc.save_path,
                    exc.underlying,
                )
                _watcher_publish(
                    "save_schema_invalid",
                    {
                        "slug": slug,
                        "save_path": str(exc.save_path),
                        "validation_error": str(exc.underlying),
                    },
                    component="session",
                    severity="error",
                )
                return [
                    _error_msg(
                        f"This save predates the current schema and cannot be "
                        f"loaded. Start a new adventure or move the save aside: "
                        f"{exc.save_path}",
                        reconnect_required=False,
                        code="save_schema_invalid",
                    )
                ]
            if saved is not None:
                snapshot = saved.snapshot
                _backfill_magic_state_on_resume(
                    snapshot=snapshot,
                    genre_pack=genre_pack,
                    world_slug=row.world_slug,
                )
                # Per-player chargen gate (playtest 2026-04-25). MP: a new
                # player_id joining a slug that already has a character must
                # route to chargen, not auto-claim the existing PC.
                #
                # Three branches:
                #   1. ``player_seats`` populated  → authoritative per-player
                #      binding; resume only if our player_id is seated.
                #   2. ``player_seats`` empty + SOLO → legacy single-PC resume;
                #      SoloSlotConflict already guarded the second connect, so
                #      ``has_character = bool(characters)`` is safe.
                #   3. ``player_seats`` empty + MP → playtest 2026-04-25 bug:
                #      Laverne's chargen completed on a pre-binding server,
                #      so the save has characters=[Laverne] but seats={}. The
                #      old fallback auto-claimed Laverne for ANY connecting
                #      player (Squiggy lands on Laverne's sheet labeled
                #      "(YOU)"). Fix: match by display_name. If display_name
                #      matches an existing character, this is the original
                #      player resuming — back-fill the seat. Otherwise it's
                #      a new joiner — route to chargen and emit a watcher
                #      event so the GM panel can see the joiner.
                _existing_char_names = {c.core.name for c in snapshot.characters if c.core.name}
                _is_mp = GameMode(row.mode) == GameMode.MULTIPLAYER
                if snapshot.player_seats:
                    has_character = player_id in snapshot.player_seats
                    gate_branch = "player_seats"
                elif not _is_mp:
                    has_character = bool(snapshot.characters)
                    gate_branch = "legacy_solo_any_character"
                elif display_name in _existing_char_names:
                    # MP back-fill: original player resuming a pre-binding
                    # save. Seat them now so subsequent joiners see the
                    # populated player_seats branch.
                    has_character = True
                    gate_branch = "mp_legacy_backfill"
                    snapshot.player_seats[player_id] = display_name
                    logger.info(
                        "session.player_seat_backfilled_on_resume "
                        "slug=%s player_id=%s character=%s",
                        slug,
                        player_id,
                        display_name,
                    )
                    _watcher_publish(
                        "session_player_seat_backfilled",
                        {
                            "slug": slug,
                            "player_id": player_id,
                            "character_name": display_name,
                            "reason": "mp_legacy_save_resume",
                        },
                        component="session",
                    )
                else:
                    # MP new joiner — route to chargen, do NOT auto-claim
                    # the existing PC. Emit a watcher event so the GM panel
                    # can see new joiners arriving.
                    has_character = False
                    gate_branch = "mp_new_joiner_chargen_required"
                    logger.info(
                        "session.mp_new_joiner_chargen_required "
                        "slug=%s player_id=%s display_name=%s "
                        "existing_characters=%s",
                        slug,
                        player_id,
                        display_name,
                        sorted(_existing_char_names),
                    )
                    _watcher_publish(
                        "mp_new_joiner_chargen_required",
                        {
                            "slug": slug,
                            "player_id": player_id,
                            "player_name": display_name,
                            "existing_character_names": sorted(_existing_char_names),
                            "character_count": len(snapshot.characters),
                        },
                        component="session",
                    )
                logger.info(
                    "session.chargen_gate slug=%s player_id=%s branch=%s "
                    "has_character=%s seat_count=%d character_count=%d",
                    slug,
                    player_id,
                    gate_branch,
                    has_character,
                    len(snapshot.player_seats),
                    len(snapshot.characters),
                )
                _watcher_publish(
                    "session_chargen_gate",
                    {
                        "slug": slug,
                        "player_id": player_id,
                        "branch": gate_branch,
                        "has_character": has_character,
                        "seat_count": len(snapshot.player_seats),
                        "character_count": len(snapshot.characters),
                        "seated_player_ids": list(snapshot.player_seats.keys()),
                    },
                    component="session",
                )
                # Rename-on-resume: pre-fix saves stored ``core.name`` as the
                # opaque player UUID because chargen used ``with_lobby_name``
                # AFTER the name fix landed. Detect the UUID pattern and
                # swap in the lobby display_name on resume, then persist so
                # the rename sticks and the next turn's PARTY_STATUS sees the
                # real name. See pingpong 2026-04-24 "Resumed character shows
                # UUID as name" (medium, user-visible everywhere).
                renamed = _rename_resumed_character_if_uuid(
                    snapshot=snapshot,
                    display_name=display_name,
                    player_id=player_id,
                )
                # ADR-037 Python port: bind the canonical snapshot to the
                # room BEFORE the rename-save below. Idempotent — if a peer
                # got here first, our load is discarded and we observe the
                # already-bound snapshot.
                room.bind_world(snapshot=snapshot, store=store, world_dir=world_dir)
                # All subsequent reads must come from the canonical room
                # binding (which may differ from our local ``snapshot`` if
                # we lost the bind race).
                snapshot = room.snapshot  # type: ignore[assignment]
                if renamed:
                    room.save()
                    logger.info(
                        "session.slug_resumed.renamed_uuid player_id=%s old=%s new=%s",
                        player_id,
                        player_id,  # equal to the pre-rename value
                        display_name,
                    )
                logger.info(
                    "session.slug_resumed genre=%s world=%s slug=%s turn=%s",
                    row.genre_slug,
                    row.world_slug,
                    slug,
                    snapshot.turn_manager.interaction,
                )
                # Story 45-10: read-side hygiene for the scrapbook subsystem.
                # Fires every save-resume; warns loudly + watcher-publishes
                # only when the scrapbook coverage diverges from the
                # narrative log. See sidequest/game/scrapbook_coverage.py.
                detect_scrapbook_coverage_gaps(
                    store=store,
                    snapshot=snapshot,
                    slug=slug,
                )
            else:
                snapshot = GameSnapshot(
                    genre_slug=row.genre_slug,
                    world_slug=row.world_slug,
                    location="Unknown",
                )
                store.init_session(row.genre_slug, row.world_slug)
                # ADR-037 Python port: bind the fresh snapshot to the room
                # so the second-connect handler observes the same object.
                room.bind_world(snapshot=snapshot, store=store, world_dir=world_dir)
                snapshot = room.snapshot  # type: ignore[assignment]
                has_character = False
                logger.info(
                    "session.slug_new_session genre=%s world=%s slug=%s",
                    row.genre_slug,
                    row.world_slug,
                    slug,
                )

            # Initialize chargen builder when entering Creating state. Slug
            # path parity with legacy branch (playtest 2026-04-23): without
            # this the client lands on an empty <CharacterCreation/> and has
            # no way to advance — there is no client-side kickoff. The lobby
            # name is the human-readable display name sent by the UI (see
            # display_name resolution above) — NOT the opaque player UUID.
            builder: CharacterBuilder | None = None
            chargen_scenes = resolve_char_creation_scenes(genre_pack, row.world_slug)
            if not has_character and chargen_scenes:
                builder = CharacterBuilder(
                    scenes=chargen_scenes,
                    rules=genre_pack.rules,
                    backstory_tables=genre_pack.backstory_tables,
                ).with_lobby_name(display_name)
                if genre_pack.equipment_tables is not None:
                    builder = builder.with_equipment_tables(genre_pack.equipment_tables)
                if genre_pack.classes:
                    builder = builder.with_classes(genre_pack.classes)

            # Opening-hook + world-context resolution (matches legacy branch).
            # Resolved once at connect time so chargen confirmation and the
            # narrator's first turn see the same directive/seed/context.
            #
            # Fresh-session guard (playtest 2026-04-30 reconnect noise): the
            # opening seed/directive are only consumed by the very first
            # narrator turn (turn 1, opening narration after chargen). On
            # reconnects after a session has begun — characters present in
            # the snapshot, interactions advanced past 0 — they ride along
            # uselessly in session_data and the ``opening_hook_selected``
            # log line fires with a freshly-rolled hook_id on every connect,
            # making it look (in the playtest pingpong) like the scene was
            # changing across tabs. Skip the dice roll when the session
            # state proves we're past the opening — the persisted
            # ``current_scene`` on the snapshot is the source of truth, not
            # a re-rolled hook.
            # Opening-hook resolution moved to chargen-completion (Task 19).
            # Connect time leaves opening slots empty; the websocket session
            # handler populates them when chargen transitions Building -> Playing.
            # Refs: docs/superpowers/specs/2026-05-01-canned-openings-design.md
            # section 2.3
            opening_seed: str | None = None
            opening_directive: str | None = None

            # MP-joiner cold-open suppression (playtest 2026-04-26 "Multiplayer
            # parallel-solo desynchronizes scene context entirely"). When a
            # second player joins an in-progress MP session, they go through
            # chargen and then fire `_run_opening_turn_narration` at the end.
            # Without this guard the narrator gets a fresh kidnapping/
            # in-medias-res cold-open directive — which it dutifully obeys by
            # inventing a NEW scene ("THE THROAT", descending into the dungeon)
            # that has nothing to do with where the existing party already is
            # ("Sinkhole Inn Room"). The shared narrator session (ADR-067)
            # remembers the prior scene, but a fresh opening_directive in the
            # Early zone overrides scene continuity.
            #
            # Detection: this player has no character yet (going to chargen)
            # AND there is at least one character already on the snapshot
            # (someone else has already started the world). When both hold
            # we're an MP joiner — suppress opening so the post-chargen
            # narration falls back to the generic "I look around and take in
            # my surroundings." which the persistent narrator handles as a
            # continuation of the existing scene.
            is_mp_joiner = not has_character and len(snapshot.characters) > 0
            if is_mp_joiner:
                _watcher_publish(
                    "mp_joiner_opening_suppressed",
                    {
                        "slug": slug,
                        "player_id": player_id,
                        "player_name": display_name,
                        "existing_character_count": len(snapshot.characters),
                        "had_seed": opening_seed is not None,
                        "had_directive": opening_directive is not None,
                    },
                    component="opening_hook",
                    severity="info",
                )
                logger.info(
                    "session.mp_joiner_opening_suppressed slug=%s "
                    "player_id=%s display_name=%s existing_chars=%d",
                    slug,
                    player_id,
                    display_name,
                    len(snapshot.characters),
                )
                opening_seed = None
                opening_directive = None
            culture_ref = resolve_culture_reference(genre_pack, row.world_slug)
            world_context: str | None = culture_ref if culture_ref else None
            audio_backend = session._build_audio_backend(row.genre_slug, genre_pack)

            # ADR-067 single-narrator-per-slug: get the canonical
            # orchestrator from the room (constructing it lazily on
            # first connect). A per-session Orchestrator would create a
            # second Claude --resume id and produce divergent narration
            # for each player on the slug — playtest 2026-04-26 "MP —
            # parallel solo games" root cause.
            shared_orchestrator = room.get_or_create_orchestrator(
                lambda: Orchestrator(client=session._client_factory())
            )
            session._session_data = _SessionData(
                genre_slug=row.genre_slug,
                world_slug=row.world_slug,
                player_name=display_name,
                player_id=player_id,
                # ADR-037 Python port: take snapshot/store directly from the
                # canonical room binding so future readers see the contract
                # explicitly. Equivalent to the local ``snapshot``/``store``
                # references after the idempotent ``bind_world`` above.
                snapshot=room.snapshot,
                store=room.store,
                genre_pack=genre_pack,
                orchestrator=shared_orchestrator,
                _room=room,  # back-reference for downstream Session access (Task D)
                builder=builder,
                opening_seed=opening_seed,
                opening_directive=opening_directive,
                world_context=world_context,
                audio_backend=audio_backend,
                game_slug=slug,
                mode=GameMode(row.mode),
                # ADR-050: pick the cooldown that matches the session mode.
                # MP defaults to 60s because turns resolve faster in group
                # play; solo gets the more responsive 30s window.
                image_pacing_throttle=(
                    ImagePacingThrottle.for_multiplayer()
                    if GameMode(row.mode) == GameMode.MULTIPLAYER
                    else ImagePacingThrottle.for_solo()
                ),
            )

            # Beneath Sünden Plan 7 (§8): wire the live dungeon look-ahead
            # worker to this session. attach_dungeon_to_session gates on
            # genre/world (returns None for non-beneath_sunden), so this is
            # a no-op for every other pack. Function-local import mirrors the
            # established lazy-import style in this handler (avoids any
            # import cycle through the dungeon package). cleanup() drains the
            # handle via detach_dungeon_from_session before the final save.
            from sidequest.dungeon.session_integration import (
                attach_dungeon_to_session,
            )

            session._session_data.lookahead_handle = await attach_dungeon_to_session(
                store=room.store,
                snapshot=room.snapshot,
                genre_pack=genre_pack,
                genre_slug=row.genre_slug,
                world_slug=row.world_slug,
                world_dir=world_dir,
            )

            # Story 24-10: load world-grounding (weather / demographics /
            # calendar) once at connect and stamp it on the session carrier.
            # _build_turn_context threads it onto every TurnContext so the
            # get_world_grounding tool returns real data. Packs/worlds with
            # no authored grounding files yield None sections (graceful).
            # Per CLAUDE.md "No Silent Fallbacks": a present-but-malformed
            # grounding file raises here and is surfaced as a typed ERROR to
            # the UI, never degraded to a silent None.
            from sidequest.game.world_grounding_loader import load_world_grounding

            try:
                grounding = load_world_grounding(
                    pack_dir=pack_dir,
                    world_dir=world_dir,
                    zone=_GROUNDING_WEATHER_ZONE,
                    season=_GROUNDING_WEATHER_SEASON,
                    seed=_GROUNDING_WEATHER_SEED,
                )
            except Exception as exc:
                logger.error(
                    "session.world_grounding_load_failed genre=%s world=%s "
                    "slug=%s error=%s",
                    row.genre_slug,
                    row.world_slug,
                    slug,
                    exc,
                )
                return [
                    _error_msg(
                        f"Failed to load world grounding for "
                        f"'{row.genre_slug}/{row.world_slug}': {exc}"
                    )
                ]
            session._session_data.weather_state = grounding.weather_state
            session._session_data.world_demographics = grounding.world_demographics
            session._session_data.world_calendar = grounding.world_calendar

            # Slug-resume LoreStore re-seed (exposed by commit 72750db).
            # ``_SessionData.lore_store`` is an in-memory
            # ``default_factory=LoreStore`` — NOT persisted to the SQLite
            # save. Only the fresh chargen flow (``_chargen_confirmation``)
            # seeded it; a returning player (``has_character=True`` —
            # i.e. the ``session.slug_resumed`` branch above) constructs a
            # brand-new empty lore_store here and never reaches
            # ``_chargen_confirmation``. Result: ``query_lore`` returned
            # ``hit_count=0`` on every resumed save and the SDK narrator
            # confabulated world canon instead of recalling it. Re-seed
            # the deterministic genre + world lore so RAG retrieval is
            # grounded on resume. Idempotent (stable ids + DuplicateLoreId
            # guard) — safe even though new chargen also seeds later. The
            # ``lore_store_loaded`` watcher event is the SAME one the
            # fresh path emits so the GM panel / Jaeger can prove lore
            # loaded on resume (CLAUDE.md OTEL lie-detector mandate).
            if has_character:
                _resume_sd = session._session_data
                genre_lore_added, world_lore_added = _seed_world_lore_on_resume(
                    lore_store=_resume_sd.lore_store,
                    genre_pack=genre_pack,
                    world_slug=row.world_slug,
                    emit=lambda **kw: _watcher_publish(
                        "lore_store_loaded",
                        {
                            "genre_slug": row.genre_slug,
                            "world_slug": row.world_slug,
                            "player_id": player_id,
                            "slug": slug,
                            "reason": "slug_resume_reseed",
                            **kw,
                        },
                        component="rag",
                    ),
                )
                logger.info(
                    "lore.store_loaded_on_resume genre=%s world=%s slug=%s "
                    "genre_fragments=%d world_fragments=%d total=%d",
                    row.genre_slug,
                    row.world_slug,
                    slug,
                    genre_lore_added,
                    world_lore_added,
                    len(_resume_sd.lore_store),
                )

            # MP-03 Task 3 + Task-17 + Task-22 ProjectionFilter Rules integration.
            session._event_log = EventLog(store)
            session._projection_cache = ProjectionCache(store)
            projection_rules = genre_pack.projection_rules
            if projection_rules is not None:
                session._projection_filter = ComposedFilter(
                    rules=projection_rules,
                    pack_slug=row.genre_slug,
                )
            else:
                session._projection_filter = ComposedFilter.with_no_genre_rules()
            session._last_seen_seq = payload.last_seen_seq or 0
            session._current_player_id = player_id
            session._state = _State.Creating if not has_character else _State.Playing

            # Connect-path auto-seat — covers two playtest regressions:
            #
            # (1) playtest 2026-04-30 "notorious_party_gate
            #     player_count=0 race" (solo). Solo sessions skip the
            #     explicit PLAYER_SEAT message that MP uses to bind a
            #     slot in the room's lobby state machine, so
            #     ``room._seated`` stays empty for the entire solo
            #     session. That breaks every consumer of
            #     ``room.non_abandoned_player_count()`` — most visibly
            #     ``orchestrator.notorious_party_gate`` in
            #     ``session_helpers._build_turn_context``, which fires
            #     its ``player_count=0 (<= 0) — impossible state``
            #     warning on turn 1.
            #
            # (2) playtest 2026-04-30 "MP playing_player_count=1 race"
            #     (returning multiplayer). When 4 MP players reconnect
            #     with ``has_character=True`` (storage clear, refresh,
            #     etc.), the only MP promotion site
            #     (``handlers/player_seat.py``) requires the client to
            #     re-send PLAYER_SEAT *and* ``session._state`` to be
            #     ``_State.Playing`` at that moment. Both conditions
            #     don't reliably coincide on reconnect, so most MP
            #     returning seats stay in CHARGEN, ``playing_player_
            #     count()`` returns 1 (or 0), and the cinematic
            #     barrier (ADR-036) fires on the first action with
            #     ``player_count=1`` — narrating solo-style and
            #     dropping the other three players' submissions.
            #
            # The lobby state machine is mode-agnostic. Auto-seat fires
            # when EITHER the session is solo (which never sends
            # PLAYER_SEAT) OR the player is returning with a committed
            # character (``has_character=True``, regardless of mode) —
            # so ``room._seated`` reflects truth. New MP players
            # without a character still claim slots via PLAYER_SEAT
            # explicitly so the lobby-roster flow preserves intent.
            #
            # Idempotent: skip when the player is already seated
            # (reconnect arriving after a successful prior connect, or
            # test fixtures that pre-seat).
            _seat_helper_room = session._room
            _is_solo = GameMode(row.mode) == GameMode.SOLO
            if (
                _seat_helper_room is not None
                and (_is_solo or has_character)
                and player_id not in _seat_helper_room.seated_player_ids()
            ):
                # ``character_slot`` follows MP semantics — the slot
                # name binds the seat to a future or existing PC. For
                # new chargen, display_name is the lobby identity. For
                # returning, snapshot.player_seats already maps
                # player_id → character_name; prefer that.
                _slot_label = (
                    snapshot.player_seats.get(player_id)
                    if has_character and snapshot.player_seats
                    else display_name
                )
                _seat_helper_room.seat(player_id, character_slot=_slot_label)
                if has_character:
                    # Returning player (solo or MP) — character is
                    # already committed, so the seat goes straight
                    # from CHARGEN to PLAYING. Mirrors the
                    # ``_handle_player_seat`` returning-player path.
                    # New solo (state=Creating) is promoted later by
                    # the chargen-complete flow's existing
                    # ``transition_to_playing`` call.
                    _seat_helper_room.transition_to_playing(player_id)
                logger.info(
                    "session.auto_seated player_id=%s slug=%s mode=%s has_character=%s slot=%r",
                    player_id,
                    slug,
                    row.mode,
                    has_character,
                    _slot_label,
                )
                _watcher_publish(
                    "session_auto_seated",
                    {
                        "slug": slug,
                        "player_id": player_id,
                        "mode": row.mode,
                        "has_character": has_character,
                        "character_slot": _slot_label,
                        "transitioned_to_playing": has_character,
                    },
                    component="session",
                    severity="info",
                )
            connected_msg = SessionEventMessage(
                type="SESSION_EVENT",  # type: ignore[arg-type]
                payload=SessionEventPayload(
                    event="connected",
                    player_name=display_name,
                    genre=row.genre_slug,
                    world=row.world_slug,
                    has_character=has_character,
                ),
                player_id=player_id,
            )

            # ADR-079 (Genre Theme System Unification): emit the genre/world
            # client_theme.css so the UI's useGenreTheme hook can inject it
            # as a <style> tag and set :root[data-genre]. World value wins;
            # genre-level CSS is the fallback. When neither is present we
            # do NOT emit — the UI keeps its pre-genre dark-mode defaults.
            #
            # OTEL: every connect emits a `genre.theme.applied` state-transition
            # event so the GM panel can prove theme wiring engaged vs. the
            # silent-fallback mishmash that shipped before this fix.
            theme_msg: SessionEventMessage | None = None
            world_obj = genre_pack.worlds.get(row.world_slug) if genre_pack else None
            world_css = world_obj.client_theme_css if world_obj is not None else None
            genre_css = genre_pack.client_theme_css if genre_pack else None
            theme_css_payload = world_css if world_css else genre_css
            theme_source = "world" if world_css else ("genre" if genre_css else "none")
            if theme_css_payload:
                theme_msg = SessionEventMessage(
                    type="SESSION_EVENT",  # type: ignore[arg-type]
                    payload=SessionEventPayload(
                        event="theme_css",
                        genre=row.genre_slug,
                        world=row.world_slug,
                        css=theme_css_payload,
                    ),
                    player_id=player_id,
                )
            _watcher_publish(
                "state_transition",
                {
                    "field": "genre_theme",
                    "op": "applied" if theme_msg is not None else "absent",
                    "genre_slug": row.genre_slug,
                    "world_slug": row.world_slug,
                    "source": theme_source,
                    "bytes": len(theme_css_payload) if theme_css_payload else 0,
                    "player_id": player_id,
                    "slug": slug,
                },
                component="genre",
            )

            # Task 19: lazy-fill projection_cache for this player if they're
            # joining a session that has events already. Subsequent reconnects
            # read from cache (Task 18) — no re-filter.
            if session._projection_cache is not None:
                from sidequest.game.projection.cache_fill import lazy_fill

                lazy_fill(
                    event_log=session._event_log,
                    cache=session._projection_cache,
                    filter_=session._projection_filter,
                    view=views.build_game_state_view(session),
                    player_id=player_id,
                )

            # MP-03 Task 4 / ProjectionFilter-Rules Task 18: replay from
            # projection_cache when present (byte-identical to what the live
            # player received). Legacy fallback runs filter live.
            #
            # Lie-detector for pingpong 2026-04-24 "Empty narrative on
            # resume" — the cache read + per-kind replay path are the
            # failure-prone hot spots. Record the cache row counts, the
            # include/exclude split, and the by-kind distribution on the
            # current span so the GM dashboard can tell at a glance
            # whether the replay had zero NARRATIONs (bug) or the UI
            # dropped them after delivery (different bug).
            replay_msgs: list[object] = []
            _replay_span = trace.get_current_span()
            _replay_cache_rows = 0
            _replay_excluded = 0
            _replay_kind_lookup_miss = 0
            _replay_skipped_internal = 0
            _replay_kinds: dict[str, int] = {}
            _replay_footnote_fact_ids_minted = 0

            # Playtest 2026-05-02 [OBS] "Scrapbook state lost on reload":
            # SCRAPBOOK_ENTRY events were emitted with image_url=None at
            # the time the narrator's structured output landed; the
            # daemon's IMAGE message arrived later via a non-event-
            # sourced broadcast and so vanished on browser reload. The
            # render-completed handler now backfills
            # ``scrapbook_entries.image_url`` for the matching turn_id.
            # Build a turn_id -> image_url map here so the replay loop
            # can JOIN it into rebuilt SCRAPBOOK_ENTRY payloads — one
            # query per reconnect, not one per row.
            _scrapbook_image_urls: dict[int, str] = {}
            if session._event_log is not None:
                try:
                    rows = session._event_log.store._conn.execute(
                        "SELECT turn_id, image_url FROM scrapbook_entries "
                        "WHERE image_url IS NOT NULL"
                    ).fetchall()
                    for _turn_id, _url in rows:
                        if isinstance(_turn_id, int) and isinstance(_url, str) and _url:
                            _scrapbook_image_urls[_turn_id] = _url
                except Exception as exc:  # noqa: BLE001 — replay must not crash on a metadata read
                    logger.warning("scrapbook.image_url_replay_lookup_failed error=%s", exc)
            if session._projection_cache is not None:
                cached_rows = session._projection_cache.read_since(
                    player_id=session._current_player_id,
                    since_seq=session._last_seen_seq,
                )
                _replay_cache_rows = len(cached_rows)
                for c in cached_rows:
                    if not c.include or c.payload_json is None:
                        _replay_excluded += 1
                        continue
                    # Need the event kind to rebuild the message — look it up.
                    # Most sessions won't have many missed events on reconnect,
                    # but this does one event-log read per cache row. Acceptable
                    # for v1; optimize to a join query if it becomes hot.
                    kind_lookup = session._event_log.read_since(since_seq=c.event_seq - 1)
                    if not kind_lookup or kind_lookup[0].seq != c.event_seq:
                        _replay_kind_lookup_miss += 1
                        continue
                    _kind = kind_lookup[0].kind
                    _built = _build_message_for_kind(
                        kind=_kind,
                        payload_json=c.payload_json,
                        seq=c.event_seq,
                    )
                    if _built is None:
                        # Internal telemetry kind (encounter*, etc.) — not
                        # client-bound. Skip without crashing replay.
                        _replay_skipped_internal += 1
                        continue
                    if (
                        _kind == "SCRAPBOOK_ENTRY"
                        and _scrapbook_image_urls
                        and getattr(_built, "payload", None) is not None
                        and getattr(_built.payload, "image_url", None) is None
                    ):
                        _t = getattr(_built.payload, "turn_id", None)
                        _url = _scrapbook_image_urls.get(_t) if isinstance(_t, int) else None
                        if _url:
                            _built = _built.model_copy(
                                update={
                                    "payload": _built.payload.model_copy(update={"image_url": _url})
                                }
                            )
                    _built, _fn_minted = _mint_replay_footnote_fact_ids(_built)
                    _replay_footnote_fact_ids_minted += _fn_minted
                    _replay_kinds[_kind] = _replay_kinds.get(_kind, 0) + 1
                    replay_msgs.append(_built)
            else:
                # Legacy fallback: no cache available, filter live (may diverge
                # from cached projections in edge cases; v1 accepts this).
                missed = session._event_log.read_since(since_seq=session._last_seen_seq)
                view = views.build_game_state_view(session)
                for event_row in missed:
                    envelope = MessageEnvelope(
                        kind=event_row.kind,
                        payload_json=event_row.payload_json,
                        origin_seq=event_row.seq,
                    )
                    dec = session._projection_filter.project(
                        envelope=envelope, view=view, player_id=session._current_player_id
                    )
                    if not dec.include:
                        continue
                    _built = _build_message_for_kind(
                        kind=event_row.kind,
                        payload_json=dec.payload_json,
                        seq=event_row.seq,
                    )
                    if _built is None:
                        _replay_skipped_internal += 1
                        continue
                    if (
                        event_row.kind == "SCRAPBOOK_ENTRY"
                        and _scrapbook_image_urls
                        and getattr(_built, "payload", None) is not None
                        and getattr(_built.payload, "image_url", None) is None
                    ):
                        _t = getattr(_built.payload, "turn_id", None)
                        _url = _scrapbook_image_urls.get(_t) if isinstance(_t, int) else None
                        if _url:
                            _built = _built.model_copy(
                                update={
                                    "payload": _built.payload.model_copy(update={"image_url": _url})
                                }
                            )
                    _built, _fn_minted = _mint_replay_footnote_fact_ids(_built)
                    _replay_footnote_fact_ids_minted += _fn_minted
                    _replay_kinds[event_row.kind] = _replay_kinds.get(event_row.kind, 0) + 1
                    replay_msgs.append(_built)

            # Pingpong 2026-04-24 "Slug-resume shows empty Narrative pane on
            # fresh browser session" — when the client's stored
            # ``last_seen_seq`` already points at (or past) the most recent
            # NARRATION, the replay loop above correctly emits zero narration
            # messages. For a live resume that's the right behavior; for a
            # fresh-browser resume the narrative pane paints blank because
            # nothing the user can read is on-screen. Backfill the most
            # recent NARRATION (plus any CHAPTER_MARKER that preceded it
            # without an intervening narration) from the cache, regardless of
            # ``last_seen_seq``, so the pane has at least the last chapter to
            # render. Bounded to 2 messages to avoid dumping a long replay.
            tail_backfill_count = 0
            if _replay_kinds.get("NARRATION", 0) == 0 and session._projection_cache is not None:
                tail_msgs = views.backfill_last_narration_block(
                    session,
                    player_id=session._current_player_id,
                )
                if tail_msgs:
                    _minted_tail: list[object] = []
                    for _tm in tail_msgs:
                        _tm, _fn_minted = _mint_replay_footnote_fact_ids(_tm)
                        _replay_footnote_fact_ids_minted += _fn_minted
                        _minted_tail.append(_tm)
                    tail_msgs = _minted_tail
                    replay_msgs.extend(tail_msgs)
                    tail_backfill_count = len(tail_msgs)
                    for msg in tail_msgs:
                        kind_name = getattr(msg, "type", "")
                        _replay_kinds[kind_name] = _replay_kinds.get(kind_name, 0) + 1

            # Always record the replay outcome — zero-replay is a bug signal
            # the GM panel needs to see (pingpong 2026-04-24 "empty narrative
            # on resume"). Attributes stay on the current mp.slug_connect
            # span; matching log line for grep-friendly tailing.
            _replay_span.set_attribute("slug_connect.replay.cache_rows", _replay_cache_rows)
            _replay_span.set_attribute("slug_connect.replay.excluded", _replay_excluded)
            _replay_span.set_attribute(
                "slug_connect.replay.kind_lookup_miss", _replay_kind_lookup_miss
            )
            _replay_span.set_attribute(
                "slug_connect.replay.skipped_internal", _replay_skipped_internal
            )
            _replay_span.set_attribute("slug_connect.replay.emitted", len(replay_msgs))
            _replay_span.set_attribute(
                "slug_connect.replay.narration_count",
                _replay_kinds.get("NARRATION", 0),
            )
            _replay_span.set_attribute(
                "slug_connect.replay.tail_backfill_count", tail_backfill_count
            )
            _replay_span.set_attribute("slug_connect.replay.last_seen_seq", session._last_seen_seq)
            _replay_span.set_attribute(
                "slug_connect.replay.footnote_fact_ids_minted",
                _replay_footnote_fact_ids_minted,
            )
            # ADR-100 Seam C visibility — mirror the live path's
            # ``state.footnote_fact_id_minted`` watcher event so the GM panel
            # sees replay/backfill mints too. Same event name, distinct
            # ``reason`` so the dashboard can split live-narration omissions
            # from old-save backfill. Zero on post-fix saves (footnotes
            # already carry fact_id); non-zero only when resuming a pre-fix
            # save — a quiet, expected backfill, not a regression signal.
            if _replay_footnote_fact_ids_minted > 0:
                _watcher_publish(
                    "state.footnote_fact_id_minted",
                    {
                        "count": _replay_footnote_fact_ids_minted,
                        "player_id": session._current_player_id,
                        "reason": "replay_backfill",
                    },
                    component="footnotes",
                )
                logger.info(
                    "state.footnote_fact_id_minted count=%d player_id=%s reason=replay_backfill slug=%s",
                    _replay_footnote_fact_ids_minted,
                    session._current_player_id,
                    slug,
                )
            logger.info(
                "slug_connect.replay cache_rows=%d excluded=%d skipped_internal=%d "
                "emitted=%d narration=%d tail_backfill=%d last_seen_seq=%d "
                "player=%s slug=%s kinds=%s",
                _replay_cache_rows,
                _replay_excluded,
                _replay_skipped_internal,
                len(replay_msgs),
                _replay_kinds.get("NARRATION", 0),
                tail_backfill_count,
                session._last_seen_seq,
                session._current_player_id,
                slug,
                dict(_replay_kinds),
            )

            # Bootstrap messages (playtest 2026-04-23 parity with legacy
            # connect path). Without these the client lands on an empty
            # <CharacterCreation/> (Creating) or stays on ConnectScreen
            # forever (Playing).
            bootstrap_msgs: list[object] = []
            if session._state is _State.Creating and builder is not None:
                bootstrap_msgs.append(builder.to_scene_message(player_id))
                # OTEL: let the GM panel verify chargen bootstrap fired —
                # lie detector for "did this subsystem actually engage?".
                # Emitted as a child of mp.slug_connect via a fresh span
                # (the mp span has already closed by this point).
                _bootstrap_tracer = trace.get_tracer("sidequest.server.session_handler")
                with _bootstrap_tracer.start_as_current_span(
                    "slug_connect.chargen_bootstrap"
                ) as _bootstrap_span:
                    _bootstrap_span.set_attribute("player_id", player_id)
                    _bootstrap_span.set_attribute("slug", slug)
                    _bootstrap_span.set_attribute("scene_index", builder.current_scene_index())
            elif session._state is _State.Playing:
                ready_msg = SessionEventMessage(
                    type="SESSION_EVENT",  # type: ignore[arg-type]
                    payload=SessionEventPayload(
                        event="ready",
                        player_name=display_name,
                        genre=row.genre_slug,
                        world=row.world_slug,
                        has_character=True,
                        initial_state=None,
                        css=None,
                        narrator_verbosity=None,
                        narrator_vocabulary=None,
                        image_cooldown_seconds=None,
                    ),
                    player_id=player_id,
                )
                bootstrap_msgs.append(ready_msg)
                logger.info(
                    "session.ready_emitted reason=slug_resume player=%s turn=%d",
                    player_id,
                    snapshot.turn_manager.interaction,
                )
                # Snapshot the resumed session so the dashboard State tab and
                # Subsystems "session" component light up on reconnect.
                # Include the full snapshot dump so the dashboard State panel
                # can paint immediately on reconnect rather than waiting for
                # the next per-turn snapshot to arrive.
                # Wave 2B (story 45-48): "current_location" is per-resuming-
                # player — use their seated character's per-character entry.
                # Fall back to ``display_name`` when the saved snapshot
                # predates the player_seats wiring (older saves and the
                # slug-resume tests seed character_locations directly without
                # populating player_seats).
                resume_char_name = snapshot.player_seats.get(player_id, "") or display_name
                resume_loc = (
                    snapshot.party_location(perspective=resume_char_name)
                    if resume_char_name
                    else snapshot.party_location()
                ) or ""
                _watcher_publish(
                    "game_state_snapshot",
                    {
                        "reason": "resume",
                        "genre_slug": row.genre_slug,
                        "world_slug": row.world_slug,
                        "player_name": display_name,
                        "player_id": player_id,
                        "turn_number": snapshot.turn_manager.interaction,
                        "snapshot": snapshot.model_dump(mode="json"),
                        "current_location": resume_loc,
                        "discovered_regions": list(snapshot.discovered_regions),
                        "npc_pool_count": len(snapshot.npc_pool),
                        "quest_log_count": len(snapshot.quest_log),
                        "lore_established_count": len(snapshot.lore_established),
                        "character_count": len(snapshot.characters),
                    },
                    component="session",
                )
                # Refresh PARTY_STATUS so the resumed client's header / sheet
                # / location update from the saved snapshot. MP: resolve
                # self by player_id (snapshot.player_seats / room seat),
                # not snapshot.characters[0] — picking the first PC mis-
                # tags every non-first player's data with their own
                # player_id (playtest 2026-04-25 "Tab 2 sees Laverne (YOU)").
                if snapshot.characters:
                    try:
                        self_char = (
                            views.resolve_self_character(session, session._session_data)
                            or snapshot.characters[0]
                        )
                        bootstrap_msgs.append(
                            views.build_session_start_party_status(
                                session, session._session_data, self_char, player_id
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("session.resume_party_status_failed error=%s", exc)
                # CHAPTER_MARKER — restore the running-header chapter title
                # on resume so the saved location shows up immediately
                # (not only after the next narration turn). Pingpong
                # 2026-04-24 "Location not rendered in the header on
                # resume": the client's ``useRunningHeader`` hook reads
                # CHAPTER_MARKER events but the server was never emitting
                # them — orphan protocol type. Emit once here; subsequent
                # turns emit their own CHAPTER_MARKER when the narrator
                # changes location (see _execute_narration_turn).
                # Wave 2B (story 45-48): chapter marker is per-resuming-
                # player — show their seated character's location, not a
                # party-frame consensus that may be empty mid-split.
                if resume_loc:
                    bootstrap_msgs.append(
                        ChapterMarkerMessage(
                            payload=ChapterMarkerPayload(
                                title=None,
                                location=_resolve_location_display(
                                    session._session_data.genre_pack
                                    if session._session_data is not None
                                    else None,
                                    row.world_slug,
                                    resume_loc,
                                ),
                            ),
                            player_id=player_id,
                        )
                    )
                # Confrontation re-emit on slug-resume (playtest 2026-05-02).
                # Without this, reloading a tab mid-confrontation drops the
                # right-pane "Confrontation" tab — the steady-state encounter
                # is still server-side but the UI overlay only mounts on a
                # fresh CONFRONTATION frame. The narration replay path
                # doesn't emit one because there was no transition during
                # the silent window. Mirror the dispatch-side build
                # (`websocket_session_handler.py:2113-2141`) so the resuming
                # client paints the overlay from the saved encounter.
                encounter = snapshot.encounter
                if (
                    encounter is not None
                    and not encounter.resolved
                    and session._session_data is not None
                    and session._session_data.genre_pack is not None
                    and session._session_data.genre_pack.rules is not None
                ):
                    from sidequest.server.dispatch.confrontation import (
                        build_confrontation_payload,
                        find_confrontation_def,
                        resolve_recipient_pc,
                    )

                    cdef = find_confrontation_def(
                        session._session_data.genre_pack.rules.confrontations,
                        encounter.encounter_type,
                    )
                    if cdef is not None:
                        try:
                            # Story 49-7: filter the bootstrap CONFRONTATION
                            # to the resuming player's class so the
                            # Confrontation tab paints with class-legal
                            # beats only, matching the live-encounter path.
                            recipient_pc, recipient_actor = resolve_recipient_pc(
                                snapshot=snapshot,
                                genre_pack=session._session_data.genre_pack,
                                player_id=player_id,
                            )
                            conf_payload_dict = build_confrontation_payload(
                                encounter=encounter,
                                cdef=cdef,
                                genre_slug=row.genre_slug,
                                recipient_pc=recipient_pc,
                                recipient_actor_name=recipient_actor,
                            )
                            bootstrap_msgs.append(
                                ConfrontationMessage(
                                    payload=ConfrontationPayload(
                                        **conf_payload_dict,
                                    ),
                                    player_id=player_id,
                                )
                            )
                            logger.info(
                                "session.slug_resume_confrontation_emitted "
                                "slug=%s encounter_type=%s player=%s",
                                slug,
                                encounter.encounter_type,
                                player_id,
                            )
                            _watcher_publish(
                                "confrontation_resume_emitted",
                                {
                                    "slug": slug,
                                    "encounter_type": encounter.encounter_type,
                                    "player_id": player_id,
                                },
                                component="confrontation",
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "session.slug_resume_confrontation_failed "
                                "encounter_type=%s error=%s",
                                encounter.encounter_type,
                                exc,
                            )
                    else:
                        logger.warning(
                            "session.slug_resume_confrontation_def_missing "
                            "encounter_type=%s genre=%s",
                            encounter.encounter_type,
                            row.genre_slug,
                        )

            # Seat backfill (playtest 2026-05-02 [BUG-LOW] — roster shows
            # peers as "creating character" forever). The connecting client
            # only learns about seated peers via SEAT_CONFIRMED broadcasts,
            # which fire at seat-claim time only. A new joiner who arrives
            # AFTER an existing player has seated never receives those
            # broadcasts, so their MultiplayerSessionStatus widget keeps
            # showing the seated peer as in-chargen until the next live
            # seat-claim (which may never come once everyone is seated).
            #
            # Fix: replay one SEAT_CONFIRMED per existing seat to the
            # connecting socket. The room.broadcast(exclude_socket_id=None)
            # path on PLAYER_SEAT also delivers the original frame to the
            # seater; this backfill targets only the new socket via the
            # bootstrap_msgs list, which the dispatcher writes onto its
            # own outbound queue (no fan-out).
            seat_backfill: list[SeatConfirmedMessage] = []
            for seated_pid, seated_slot in snapshot.player_seats.items():
                seat_backfill.append(
                    SeatConfirmedMessage(
                        payload=SeatConfirmedPayload(
                            player_id=seated_pid,
                            character_slot=seated_slot,
                        ),
                    )
                )
            if seat_backfill:
                logger.info(
                    "session.seat_backfill_emitted slug=%s player_id=%s count=%d seated=%s",
                    slug,
                    player_id,
                    len(seat_backfill),
                    list(snapshot.player_seats.keys()),
                )
                _watcher_publish(
                    "session_seat_backfill_emitted",
                    {
                        "slug": slug,
                        "player_id": player_id,
                        "count": len(seat_backfill),
                        "seated_player_ids": list(snapshot.player_seats.keys()),
                    },
                    component="session",
                )

            theme_prefix: list[object] = [theme_msg] if theme_msg is not None else []
            return [
                connected_msg,
                *theme_prefix,
                *bootstrap_msgs,
                *seat_backfill,
                *replay_msgs,
            ]

        # Story 45-26: legacy (genre, world, player_name) connect path
        # was deleted alongside the legacy /api/saves/* REST routes.
        # Clients must mint a slug via POST /api/games and connect with
        # ``payload.game_slug``. UI has done this exclusively since MP-03.
        return [
            _error_msg(
                "SESSION_EVENT{connect} requires payload.game_slug — "
                "the legacy genre+world+player_name connect path was "
                "removed in 45-26. Mint a slug via POST /api/games."
            )
        ]


HANDLER = ConnectHandler()
