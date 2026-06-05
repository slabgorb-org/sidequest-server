"""Per-connection WebSocket session handler.

Extracted from ``session_handler.py``; helpers (``_State``, ``_SessionData``,
etc.) remain re-exported from there. Tracer name is preserved
(``sidequest.server.session_handler``) so OTEL span sources do not rename.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from opentelemetry import trace

if TYPE_CHECKING:
    from sidequest.game.retrieval_orchestration import RetrievedEntities
    from sidequest.handlers.base import MessageHandler
    from sidequest.server.session_room import RoomRegistry, SessionRoom

from sidequest.agents.claude_client import LlmClient
from sidequest.agents.dispatch_engagement_watcher import (
    run_dispatch_engagement_watcher,
)
from sidequest.agents.intent_router import IntentRouterFailure
from sidequest.agents.llm_factory import _INTENT_ROUTER_MODEL, build_llm_client
from sidequest.agents.orchestrator import TurnContext
from sidequest.daemon_client import (
    DaemonClient,
    DaemonRequestError,
    DaemonUnavailableError,
    render_enabled,
)
from sidequest.game.event_log import EventLog
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection_filter import ProjectionFilter
from sidequest.game.session import (
    GameSnapshot,
    NarrativeEntry,
)
from sidequest.game.shared_world_delta import (
    build_shared_world_delta,
)
from sidequest.game.tension_tracker import RoundResult
from sidequest.game.world_materialization import (
    recompute_arc_history,
    should_recompute_arc,
)
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS
from sidequest.genre.models.world import NavigationMode
from sidequest.protocol import GameMessage
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    ChapterMarkerMessage,
    ChapterMarkerPayload,
    ConfrontationPayload,
    ImageMessage,
    ImagePayload,
    NarrationEndMessage,
    NarrationEndPayload,
    NarrationMessage,
    NarrationPayload,
    NarrationSegmentPayload,
    RenderQueuedMessage,
    RenderQueuedPayload,
    ScrapbookEntryPayload,
    SecretNotePayload,
    SessionEventPayload,
    TurnStatusMessage,
    TurnStatusPayload,
)
from sidequest.protocol.models import (
    Footnote,
)
from sidequest.protocol.types import NonBlankString
from sidequest.server import intent_router_pass, views
from sidequest.server.dispatch.opening import (
    record_opening_played,
)
from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass
from sidequest.server.narration_apply import (
    _apply_narration_result_to_snapshot,
    _handshake_resolved_tropes,
)
from sidequest.server.session_helpers import (
    _build_turn_context,
    _error_msg,
    _render_url_from_path,
    _resolve_acting_character_name,
    _resolve_location_display,
    build_secret_note_events,
)
from sidequest.server.session_state import (
    _build_pc_descriptor,
    _hash_snapshot,
    _SessionData,
    _shared_world_delta_to_state_delta,
    _State,
)
from sidequest.server.utils import slugify_player_name as _slugify_player_name
from sidequest.telemetry.phase_timing import PhaseTimings
from sidequest.telemetry.spans import (
    encounter_momentum_broadcast_span,
    orchestrator_process_action_span,
    round_invariant_span,
    turn_span,
)
from sidequest.telemetry.spans.intent_router import intent_router_decompose_span
from sidequest.telemetry.turn_record import PatchSummary, TurnRecord
from sidequest.telemetry.validator import Validator
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)

# Preserve the original tracer name so OTEL span sources do not rename.
tracer = trace.get_tracer("sidequest.server.session_handler")


def perspective_character_name(sd) -> str:
    """The seated character name to use as a party_location perspective key.

    Story 67-6: the POV key is the seated CHARACTER, resolved from
    snapshot.player_seats[player_id] — never the player identity. Falls back to
    sd.player_name (which is itself the character name pre-seat) when no seat is
    bound yet, preserving prior behavior.
    """
    return sd.snapshot.player_seats.get(sd.player_id, sd.player_name)


def _drive_session_tension_tracker(
    sd: _SessionData,
    snapshot: GameSnapshot,
    *,
    encounter_resolved_this_turn: bool,
) -> None:
    """Feed the per-session :class:`TensionTracker` one observation per turn.

    ADR-024 producer wiring (story 81-2). The narrator improvises pacing on
    *every* turn, so the dual-track signal must update every turn — not only on
    combat turns — which is why this is driven here in the handler (a quiet turn
    is a Boring observation) rather than buried in the LLM-gated narration-apply
    path. ``observe()`` records the action event, ages the spike, AND emits the
    ``tension:round_observed`` watcher event the GM panel reads (the only
    emitting path — we reuse it rather than add a parallel telemetry channel).

    Honest mapping from resolved turn state to the tracker's inputs:

    - **stakes track** — the acting player's real HP ratio via ``update_stakes``.
    - **action track + drama** — ``observe()`` classifies the turn from real
      encounter signals: a combatant defeated *this* turn is a dramatic kill;
      the lowest seated combatant HP ratio drives NearMiss; otherwise a quiet
      turn ramps the gambler's Boring streak.

    Per-turn HP-delta ``damage_events`` are intentionally NOT synthesized (no
    start/end HP capture this story) — the relative-magnitude math is covered by
    the tracker's own unit tests; see the Dev deviation. Input reads are
    None/zero-guarded so the tracker is never called with invalid args (e.g.
    ``update_stakes`` with ``max_hp <= 0``); the single call site additionally
    wraps this in try/except so a watcher-hub hiccup can't crash the turn
    (ADR-006 graceful degradation).
    """
    tracker = sd.tension_tracker

    # Stakes track from the acting player's real HP, when seated with a pool.
    pc = snapshot.find_creature_core(sd.player_name)
    if pc is not None and pc.hp.max > 0:
        tracker.update_stakes(pc.hp.current, pc.hp.max)

    killed: str | None = None
    lowest_hp_ratio: float | None = None
    enc = snapshot.encounter
    if enc is not None:
        ratios = [
            core.hp.current / core.hp.max
            for actor in enc.actors
            if (core := snapshot.find_creature_core(actor.name)) is not None and core.hp.max > 0
        ]
        if ratios:
            lowest_hp_ratio = min(ratios)
        # A side defeated *this* turn is a dramatic kill (real resolution). The
        # this-turn nuance keeps the spike from re-firing while a resolved
        # encounter lingers. Empty string still counts as a kill in classify.
        if encounter_resolved_this_turn and enc.outcome in (
            "player_victory",
            "opponent_victory",
        ):
            defeated_side = "opponent" if enc.outcome == "player_victory" else "player"
            killed = next((a.name for a in enc.actors if a.side == defeated_side), "")

    tracker.observe(
        RoundResult(round=snapshot.turn_manager.interaction),
        killed=killed,
        lowest_hp_ratio=lowest_hp_ratio,
    )


# --- Extracted handler helpers (moved to websocket_handlers/) -------------
# Free functions/mixins moved to sibling modules under ``websocket_handlers/``,
# re-imported here so methods + external importers resolve the names unchanged.
from sidequest.server.websocket_handlers.audio_mixin import (  # noqa: E402
    AudioDispatchMixin,
)

# Chargen worker methods live in a mixin; inherited so session._chargen_* calls
# resolve via the MRO.
from sidequest.server.websocket_handlers.chargen_mixin import (  # noqa: E402
    CharGenMixin,
)
from sidequest.server.websocket_handlers.map_emit import (  # noqa: E402
    _maybe_emit_cartography_map,
    _maybe_emit_dungeon_map,
    _maybe_emit_location_description,
    _maybe_emit_location_overlay_changed,
    _maybe_emit_tactical_grid,
)
from sidequest.server.websocket_handlers.quests_emit import (  # noqa: E402
    _maybe_emit_quests,
)
from sidequest.server.websocket_handlers.relationships_emit import (  # noqa: E402
    _maybe_emit_relationships,
)


class WebSocketSessionHandler(AudioDispatchMixin, CharGenMixin):
    """Per-connection session: state machine + dispatch.

    Created fresh per WebSocket connection by the /ws endpoint factory.
    """

    def __init__(
        self,
        *,
        claude_client_factory: Callable[[], LlmClient] | None = None,
        genre_pack_search_paths: list[Path] | None = None,
        save_dir: Path,
        validator: Validator | None = None,
    ) -> None:
        self._client_factory: Callable[[], LlmClient] = (
            claude_client_factory
            if claude_client_factory is not None
            else cast(
                "Callable[[], LlmClient]",
                lambda: build_llm_client(purpose="narrator"),
            )
        )
        self._search_paths: list[Path] = (
            genre_pack_search_paths
            if genre_pack_search_paths is not None
            else DEFAULT_GENRE_PACK_SEARCH_PATHS
        )
        self._save_dir = save_dir
        self._validator: Validator | None = validator
        self._state = _State.AwaitingConnect
        self._session_data: _SessionData | None = None
        # Room context — populated by attach_room_context() during the
        # ws_endpoint lifecycle. Absent means driven outside it (unit tests);
        # the slug-connect branch rejects that loudly (no silent room-wiring skip).
        self._room_registry: RoomRegistry | None = None
        self._socket_id: str | None = None
        self._out_queue: asyncio.Queue[object] | None = None
        self._room: SessionRoom | None = None
        self._player_identity: str | None = None
        self._player_identity_source: str | None = None
        # Bound in the slug-connect branch. The legacy genre/world connect path
        # leaves them None; _emit_event then falls back to a plain message
        # without seq (a real production path, not a test-only skip).
        self._event_log: EventLog | None = None
        self._projection_filter: ProjectionFilter | None = None
        self._projection_cache: ProjectionCache | None = None
        # Story 61-followup-C: ws_endpoint reads this after cleanup() to decide
        # whether to fire room.close_store(). The cleanup save block swallows
        # exceptions; exposing the failure here lets ws_endpoint skip
        # close_store and preserve the canonical store for a retry.
        self.last_save_failure: Exception | None = None

    # ------------------------------------------------------------------
    # Room context (MP-02 Task 2)
    # ------------------------------------------------------------------

    def attach_room_context(
        self,
        *,
        registry: RoomRegistry,
        socket_id: str,
        out_queue: asyncio.Queue[object],
        player_identity: str | None = None,
        player_identity_source: str | None = None,
    ) -> None:
        """Attach the RoomRegistry, socket_id, and per-socket outbound queue.

        Called by ws_endpoint after accept(). out_queue is the asyncio.Queue
        the writer task drains. All three are required; the slug-connect branch
        fails loudly if this was not called.

        player_identity and player_identity_source are optional (default None)
        so existing callers/tests that omit them continue to work.
        """
        self._room_registry = registry
        self._socket_id = socket_id
        self._out_queue = out_queue
        self._player_identity = player_identity
        self._player_identity_source = player_identity_source

    def current_room(self) -> SessionRoom | None:
        """Return the room this handler is currently registered in, or None."""
        return self._room

    # ------------------------------------------------------------------
    # EventLog fan-out helper (MP-03 Task 3)
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        kind: str,
        payload_model: object,
        *,
        author_player_id: str | None = None,
        per_recipient_payload: Callable[[str], object] | None = None,
    ) -> object:
        """Persist + fan-out an event. Delegates to ``emitters.emit_event``.

        ``author_player_id`` (ADR-105 Track A): when set, this is a shared
        merged-MP turn whose driver is merely the last-submitter, so the
        emitter is projected like any recipient. ``None`` (solo/legacy)
        preserves the raw-bypass + lazy_fill invariant.

        ``per_recipient_payload`` (Story 59-16): a supplier delivering one
        class-filtered CONFRONTATION frame to every connected socket while
        the canonical union is persisted to the EventLog only.
        """
        from sidequest.server import emitters

        return emitters.emit_event(
            self,
            kind,
            payload_model,
            author_player_id=author_player_id,
            per_recipient_payload=per_recipient_payload,
        )

    def _dispatch_pending_magic_frames(self, snapshot: GameSnapshot) -> None:
        """Phase 5 (Story 47-3): drain pending magic-confrontation queues.

        Dispatches ``pending_magic_auto_fires`` (CONFRONTATION starts) and
        ``pending_magic_confrontation_outcome`` as outbound frames, resetting
        both. Emitted via ``_emit_event`` so they hit the EventLog + projection;
        dispatch errors propagate (broken queue is loud per CLAUDE.md).
        """
        # Pop-as-you-go so a malformed entry's ValidationError doesn't strand
        # the rest of the queue or re-fire valid entries each dispatch tick.
        if snapshot.pending_magic_auto_fires:
            from sidequest.protocol.messages import ConfrontationPayload

            queue = snapshot.pending_magic_auto_fires
            while queue:
                raw = queue.pop(0)
                try:
                    payload = ConfrontationPayload(**raw)
                except Exception:
                    # Fail loud: surface the malformed entry to the GM panel +
                    # log, drop it (already popped), keep draining.
                    logger.error(
                        "magic.dispatch_payload_invalid kind=CONFRONTATION raw=%r",
                        raw,
                    )
                    _watcher_publish(
                        "state_transition",
                        {
                            "field": "magic_state",
                            "op": "dispatch_payload_invalid",
                            "kind": "CONFRONTATION",
                            "raw": raw,
                        },
                        component="magic",
                        severity="error",
                    )
                    continue
                self._emit_event("CONFRONTATION", payload)

        # CONFRONTATION_OUTCOME (reveal panel, always shown). Reset BEFORE emit
        # so a ValidationError doesn't strand the payload for the next tick.
        if snapshot.pending_magic_confrontation_outcome is not None:
            from sidequest.protocol.messages import ConfrontationOutcomePayload

            raw_outcome = snapshot.pending_magic_confrontation_outcome
            snapshot.pending_magic_confrontation_outcome = None
            try:
                outcome_payload = ConfrontationOutcomePayload(**raw_outcome)
            except Exception:
                logger.error(
                    "magic.dispatch_payload_invalid kind=CONFRONTATION_OUTCOME raw=%r",
                    raw_outcome,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "magic_state",
                        "op": "dispatch_payload_invalid",
                        "kind": "CONFRONTATION_OUTCOME",
                        "raw": raw_outcome,
                    },
                    component="magic",
                    severity="error",
                )
            else:
                self._emit_event("CONFRONTATION_OUTCOME", outcome_payload)

    # ------------------------------------------------------------------
    # Scrapbook entry emission (pingpong 2026-04-26 [S3-REGRESSION])
    # ------------------------------------------------------------------

    def _emit_scrapbook_entry(
        self,
        *,
        sd: _SessionData,
        snapshot: GameSnapshot,
        result: object,
        render_status: str = "rendered",
    ) -> None:
        """Persist + emit a scrapbook entry. Delegates to ``emitters.emit_scrapbook_entry``.

        ``render_status`` (Story 45-30 + 45-31): ``"rendered"``,
        ``"skipped_policy"`` (trigger policy NONE_POLICY), ``"failed"`` (daemon
        errored), ``"unavailable"`` (daemon UNRESPONSIVE). Daemon-unavailable
        wins over policy — no render is coming either way.
        """
        from sidequest.server import emitters

        emitters.emit_scrapbook_entry(
            self,
            sd=sd,
            snapshot=snapshot,
            result=result,
            render_status=render_status,
        )

    def _persist_scrapbook_entry(self, payload: ScrapbookEntryPayload) -> None:
        """Insert a scrapbook row. Delegates to ``emitters.persist_scrapbook_entry``."""
        from sidequest.server import emitters

        emitters.persist_scrapbook_entry(self, payload)

    # ------------------------------------------------------------------
    # Public entrypoints
    # ------------------------------------------------------------------

    @property
    def session_data(self) -> _SessionData | None:
        """Public read accessor for session state (used by tests and GM panel)."""
        return self._session_data

    async def handle_message(self, msg: GameMessage) -> list[object]:
        """Dispatch an inbound message; return outbound protocol message objects.

        Looks up the message type in the ``_MESSAGE_HANDLERS`` registry and
        forwards to the corresponding handler under :mod:`sidequest.handlers`.
        The thin ``_handle_X`` methods remain as a test-friendly public API.
        """
        msg_type: str = msg.type  # type: ignore[attr-defined]

        handler = type(self)._message_handler_for(msg_type)
        if handler is None:
            logger.warning(
                "session.unhandled_message_type type=%s state=%s",
                msg_type,
                self._state.name,
            )
            return [_error_msg(f"Unsupported message type in Phase 1: {msg_type}")]
        return await handler.handle(self, msg)

    @classmethod
    def _message_handler_for(cls, msg_type: str) -> MessageHandler | None:
        """Lazy-built registry of message-type → handler singleton.

        Built on first call to avoid class-definition-time imports of the
        handler modules (which would create a circular reference, since they
        import this class). Cached on the class.
        """
        registry = getattr(cls, "_MESSAGE_HANDLERS", None)
        if registry is None:
            from sidequest.handlers.action_reveal import HANDLER as ACTION_REVEAL_HANDLER
            from sidequest.handlers.character_creation import HANDLER as CHARACTER_CREATION_HANDLER
            from sidequest.handlers.check_throw import HANDLER as CHECK_THROW_HANDLER
            from sidequest.handlers.client_error import HANDLER as CLIENT_ERROR_HANDLER
            from sidequest.handlers.dice_throw import HANDLER as DICE_THROW_HANDLER
            from sidequest.handlers.journal_request import HANDLER as JOURNAL_REQUEST_HANDLER
            from sidequest.handlers.orbital_intent import HANDLER as ORBITAL_INTENT_HANDLER
            from sidequest.handlers.player_action import HANDLER as PLAYER_ACTION_HANDLER
            from sidequest.handlers.player_seat import HANDLER as PLAYER_SEAT_HANDLER
            from sidequest.handlers.session_event import HANDLER as SESSION_EVENT_HANDLER
            from sidequest.handlers.yield_action import HANDLER as YIELD_HANDLER

            registry = {
                "SESSION_EVENT": SESSION_EVENT_HANDLER,
                "PLAYER_ACTION": PLAYER_ACTION_HANDLER,
                "CHARACTER_CREATION": CHARACTER_CREATION_HANDLER,
                "PLAYER_SEAT": PLAYER_SEAT_HANDLER,
                "DICE_THROW": DICE_THROW_HANDLER,
                "CHECK_THROW": CHECK_THROW_HANDLER,
                "CLIENT_ERROR": CLIENT_ERROR_HANDLER,
                "YIELD": YIELD_HANDLER,
                "ORBITAL_INTENT": ORBITAL_INTENT_HANDLER,
                "ACTION_REVEAL": ACTION_REVEAL_HANDLER,
                "JOURNAL_REQUEST": JOURNAL_REQUEST_HANDLER,
            }
            cls._MESSAGE_HANDLERS = registry
        return registry.get(msg_type)

    async def cleanup(self) -> None:
        """Called on disconnect — persist current state if in Playing."""
        if self._session_data is not None:
            # Cancel any in-flight embed worker first so it cannot write to an
            # orphaned lore_store after store.close(). Await + swallow
            # CancelledError so disconnect never raises; log real worker bugs.
            embed_task = self._session_data.embed_task
            if embed_task is not None and not embed_task.done():
                embed_task.cancel()
                try:
                    await embed_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001 — cleanup must proceed
                    logger.warning(
                        "session.embed_task_cleanup_error type=%s error=%s",
                        type(exc).__name__,
                        exc,
                        exc_info=True,
                    )
            # Beneath Sünden Plan 7 (§8): drain the dungeon look-ahead worker
            # before the final save. Null-safe (None off beneath_sunden); drains
            # only, must NOT close the store (the room owns it). Local import
            # avoids a dungeon-package import cycle.
            from sidequest.dungeon.session_integration import (
                detach_dungeon_from_session,
            )

            await detach_dungeon_from_session(self._session_data.lookahead_handle)
            try:
                # ADR-037: room owns the canonical snapshot, so room.save()
                # persists it once. Legacy non-slug path uses the per-session store.
                if self._room is not None:
                    self._room.save()
                else:
                    self._session_data.repository.save(self._session_data.snapshot)
                logger.info(
                    "session.disconnect_save genre=%s world=%s player=%s "
                    "char_count=%d seat_count=%d",
                    self._session_data.genre_slug,
                    self._session_data.world_slug,
                    self._session_data.player_name,
                    len(self._session_data.snapshot.characters),
                    len(self._session_data.snapshot.player_seats),
                )
            except Exception as exc:
                logger.error("session.disconnect_save_failed error=%s", exc)
                # Story 61-followup-C: expose the swallowed save failure so
                # ws_endpoint's teardown gate can skip close_store() — closing
                # the canonical store after a lost save would make the loss permanent.
                self.last_save_failure = exc

            # Story 75-15: persist the lore_store on disconnect — the in-memory
            # store is not part of the snapshot, so the save above does not cover
            # it. ISOLATED in its own try/except: a lore-only failure must NOT set
            # last_save_failure (that gates close_store on the canonical snapshot
            # save, which is a strictly more important write).
            try:
                self._session_data.repository.save_lore_fragments(self._session_data.lore_store)
            except Exception as exc:  # noqa: BLE001 — lore persist must not block teardown
                logger.error("lore.disconnect_persist_failed error=%s", exc)
                _watcher_publish(
                    "lore_persist_failed",
                    {"scope": "disconnect", "error": str(exc)},
                    component="rag",
                    severity="error",
                )

            # Story 45-31: post-session render diagnostic — JSON snapshot of the
            # render worker's lifetime for later diagnosis. Best-effort; must
            # never raise back to the WebSocket layer.
            try:
                from datetime import UTC
                from datetime import datetime as _dt

                from sidequest.daemon_client.state_mirror import get_mirror
                from sidequest.server.render_diagnostics import (
                    write_session_diagnostic,
                )

                room_slug_for_diag: str | None = None
                if self._room is not None:
                    room_slug_for_diag = getattr(self._room, "slug", None)
                if not room_slug_for_diag:
                    # Legacy/no-slug path — namespace by genre+player.
                    room_slug_for_diag = (
                        (
                            f"{self._session_data.genre_slug or 'unknown'}-"
                            f"{self._session_data.player_id or 'unknown'}"
                        )
                        .replace("/", "-")
                        .replace("\\", "-")
                    )

                _mirror = get_mirror()
                snapshot_payload = {
                    "heartbeat_history": [
                        {
                            "queue": q,
                            "state": _mirror.state(q).value,
                            "queue_depth": _mirror.queue_depth(q),
                        }
                        for q in ("image", "embed")
                    ],
                    "enqueue_count": int(self._session_data.render_enqueue_count),
                    "backpressure_warn_count": int(
                        self._session_data.render_backpressure_warn_count
                    ),
                    "unresponsive_windows": [
                        {
                            "count": int(self._session_data.render_unresponsive_window_count),
                        }
                    ]
                    if self._session_data.render_unresponsive_window_count
                    else [],
                    "last_successful_render_id": (self._session_data.last_successful_render_id),
                    "last_successful_render_ts": (self._session_data.last_successful_render_ts_iso),
                    "last_heartbeat_ts": _mirror.last_heartbeat_ts(),
                }
                write_session_diagnostic(
                    room_slug=room_slug_for_diag,
                    session_end_iso=_dt.now(UTC).isoformat(),
                    snapshot=snapshot_payload,
                )
            except Exception as _diag_exc:  # noqa: BLE001 — diagnostic must never crash teardown
                logger.warning("render.session_diagnostic_failed err=%s", _diag_exc)
            finally:
                # ADR-037: a room-bound session shares the room's SqliteStore,
                # so closing it from one session's cleanup() breaks every other
                # session's room.save() (playtest 2026-04-25: "Cannot operate on
                # a closed database"). The room closes its store at teardown.
                # Legacy non-slug path owns + closes its per-session store here.
                if self._room is None:
                    with contextlib.suppress(Exception):
                        self._session_data.repository.close()

    # ------------------------------------------------------------------
    # PLAYER_SEAT dispatch (MP-02 Task 5)
    # ------------------------------------------------------------------

    async def _handle_player_seat(self, msg: GameMessage) -> list[object]:
        """Handle PLAYER_SEAT — delegates to ``sidequest.handlers.player_seat.HANDLER``."""
        from sidequest.handlers.player_seat import HANDLER

        return await HANDLER.handle(self, msg)

    # ------------------------------------------------------------------
    # DICE_THROW dispatch
    # ------------------------------------------------------------------

    async def _handle_dice_throw(self, msg: GameMessage) -> list[object]:
        """Handle DICE_THROW — delegates to ``sidequest.handlers.dice_throw.HANDLER``."""
        from sidequest.handlers.dice_throw import HANDLER

        return await HANDLER.handle(self, msg)

    # ------------------------------------------------------------------
    # YIELD dispatch (dual-track momentum Phase 3)
    # ------------------------------------------------------------------

    async def _handle_yield(self, msg: GameMessage) -> list[object]:
        """Handle YIELD — delegates to ``sidequest.handlers.yield_action.HANDLER``."""
        from sidequest.handlers.yield_action import HANDLER

        return await HANDLER.handle(self, msg)

    # ------------------------------------------------------------------
    # SESSION_EVENT dispatch
    # ------------------------------------------------------------------

    async def _handle_session_event(self, msg: GameMessage) -> list[object]:
        """Handle SESSION_EVENT — delegates to ``sidequest.handlers.session_event.HANDLER``."""
        from sidequest.handlers.session_event import HANDLER

        return await HANDLER.handle(self, msg)

    async def _handle_connect(
        self,
        payload: SessionEventPayload,
        player_id: str,
    ) -> list[object]:
        """Handle connect sub-event — delegates to ``sidequest.handlers.connect.HANDLER``."""
        from sidequest.handlers.connect import HANDLER

        return await HANDLER.handle(self, payload, player_id)

    # ------------------------------------------------------------------
    # CHARACTER_CREATION dispatch
    # ------------------------------------------------------------------

    async def _handle_character_creation(self, msg: GameMessage) -> list[object]:
        """Handle CHARACTER_CREATION — delegates to ``sidequest.handlers.character_creation.HANDLER``."""
        from sidequest.handlers.character_creation import HANDLER

        return await HANDLER.handle(self, msg)

    # ------------------------------------------------------------------
    # PLAYER_ACTION dispatch
    # ------------------------------------------------------------------

    async def _handle_player_action(self, msg: GameMessage) -> list[object]:
        """Handle PLAYER_ACTION — delegates to ``sidequest.handlers.player_action.HANDLER``."""
        from sidequest.handlers.player_action import HANDLER

        return await HANDLER.handle(self, msg)

    # ------------------------------------------------------------------
    # Narration execution — shared between player_action and opening turn
    # ------------------------------------------------------------------

    async def _execute_narration_turn(
        self,
        sd: _SessionData,
        action: str,
        turn_context: TurnContext,
        *,
        is_opening_turn: bool = False,
    ) -> list[object]:
        """Run one narration turn: orchestrator call, snapshot mutation,
        persistence, NARRATION + NARRATION_END message build.

        Shared by :meth:`_handle_player_action` and
        :meth:`_run_opening_turn_narration`; the caller owns TurnContext so each
        entrypoint sets its own per-turn fields.

        ``is_opening_turn`` (Story 45-5 / ADR-051): the opening scene-set skips
        ``record_interaction()`` so post-chargen state stays at exactly
        ``(round=1, interaction=1)`` and the 45-11 round_invariant still holds.
        The first PLAYER_ACTION turn advances both counters in lockstep.
        """
        snapshot = sd.snapshot
        snapshot_before_hash = _hash_snapshot(snapshot)
        # Reuse the caller's PhaseTimings when attached so pre-narrator phases
        # land in the same dict the dashboard reads; otherwise construct here.
        if isinstance(turn_context.phase_timings, PhaseTimings) and (
            turn_context.phase_timings is not PhaseTimings.NULL
        ):
            timings = turn_context.phase_timings
        else:
            timings = PhaseTimings(action_received_monotonic=time.monotonic())
            turn_context.phase_timings = timings
        submitted = False
        result = None  # populated by run_narration_turn; None on degraded paths
        # Story 45-20: capture trope-status baseline BEFORE any apply step so
        # the post-record_interaction handshake can diff it to detect tropes
        # that flipped to "resolved" this turn. Capturing late would mask the diff.
        trope_status_baseline: dict[str, str] = {t.id: t.status for t in snapshot.active_tropes}
        # Capture the watcher→OTLP synthetic-span counter at turn start so the
        # finally-block can log the per-turn delta — gives Jaeger-empty turns a
        # grep-able truth-value for "did the bridge fire?" (playtest 2026-04-30).
        from sidequest.telemetry.watcher_hub import synthetic_spans_count  # noqa: PLC0415

        bridge_minted_at_start = synthetic_spans_count()
        try:
            with turn_span(
                turn_id=snapshot.turn_manager.interaction,
                player_id=sd.player_id,
                agent_name="narrator",
                genre=sd.genre_slug,
                world=sd.world_slug,
                action_len=len(action),
            ):
                # Monster Manual injection (ADR-059). Materialize Manual NPCs +
                # encounter creatures into snapshot.npcs BEFORE the narrator runs
                # so the gaslighting doctrine delivers them as world truth, not
                # appended "available list" text. Refresh TurnContext.npcs after.
                from sidequest.server.dispatch import monster_manual_inject

                manual = monster_manual_inject.ensure_loaded(sd)
                if manual is not None:
                    mm_location = (
                        turn_context.current_location
                        if isinstance(turn_context.current_location, str)
                        else ""
                    )
                    mm_injected = monster_manual_inject.inject(
                        sd,
                        snapshot,
                        current_location=mm_location,
                        in_combat=bool(turn_context.in_combat),
                    )
                    # Plain-text proof (CLAUDE.md OTEL principle): log the actual
                    # patch count so a GM reading server.log can tell the Manual
                    # materialized vs. the narrator improvising creatures.
                    logger.info(
                        "monster_manual.injected genre=%s world=%s "
                        "player_id=%s turn=%s in_combat=%s patches=%d",
                        sd.genre_slug,
                        sd.world_slug,
                        sd.player_id,
                        turn_context.turn_number,
                        bool(turn_context.in_combat),
                        mm_injected,
                    )
                    turn_context.npcs = list(snapshot.npcs)
                    # _build_turn_context snapshots monster_manual at the caller,
                    # BEFORE ensure_loaded() above populates it; refresh here or
                    # the orchestrator gets None and lookup_monster goes dead
                    # (playtest 2026-05-17).
                    turn_context.monster_manual = sd.monster_manual

                # Story 22-3: bootstrap the seed-trope deck for a fresh session.
                # Idempotent. Session id from room slug / sd.game_slug, falling
                # back to a deterministic id for non-slug-connect paths.
                from sidequest.game.seed_tick import ensure_initial_draw  # noqa: PLC0415

                if self._room is not None:
                    seed_session_id = self._room.slug
                elif sd.game_slug is not None:
                    seed_session_id = sd.game_slug
                else:
                    seed_session_id = f"{sd.genre_slug}::{sd.world_slug}::{sd.player_id}"
                ensure_initial_draw(
                    snapshot,
                    sd.genre_pack,
                    session_id=seed_session_id,
                    now_turn=snapshot.turn_manager.interaction,
                )
                # Refresh TurnContext from the post-bootstrap snapshot so
                # build_narrator_prompt sees the freshly drawn seeds.
                turn_context.snapshot = snapshot

                # Intent Router pre-narrator pass (Story 59-4, ADR-113): the
                # router classifies the action and the dispatch bank engages
                # engines on the canonical snapshot BEFORE the narrator runs, so
                # the narrator narrates already-real state. IntentRouterFailure
                # (after bounded retry) propagates to the turn-failure path — NO
                # silent narrator-only fallback. The factory call is module-level
                # so tests can monkeypatch it (must not spawn a real Claude client).
                _acting_player_name = snapshot.player_seats.get(sd.player_id, "") or sd.player_id
                _additional_player_names = [
                    name
                    for pid, name in snapshot.player_seats.items()
                    if pid != sd.player_id and name and name != _acting_player_name
                ]
                _intent_router = intent_router_pass.build_intent_router_for_session()
                # Opt-in degraded path: when SIDEQUEST_INTENT_ROUTER_DEGRADE_ON_FAIL
                # is set, an IntentRouterFailure is logged LOUDLY and the turn
                # continues with dispatch_package=None (pre-ADR-113 behavior, an
                # operator opt-in, NOT a silent fallback). Default preserves the
                # ADR-113 fail-loud contract.
                try:
                    # Movement subsystem context: the dungeon graph store +
                    # palette + worker handle live on the lookahead handle (set
                    # for beneath_sunden, None elsewhere). None on non-procedural
                    # worlds → handler fails loud with no_dungeon_store.
                    _lookahead_handle = getattr(sd, "lookahead_handle", None)
                    _dungeon_store = (
                        _lookahead_handle.persistence if _lookahead_handle is not None else None
                    )
                    _dungeon_palette = (
                        _lookahead_handle.palette if _lookahead_handle is not None else None
                    )
                    # Stamp the dispatch-bank/equip spans with the turn number
                    # turn_complete WILL emit (interaction+1 for a player turn —
                    # record_interaction() runs below, after this pass). Without
                    # it the spans grid one column to the left and the GM panel
                    # shows intent_router/inventory dark on the resolving turn
                    # (off-by-one, DRIVER 2026-06-04).
                    _dispatch_turn_number = intent_router_pass.effective_dispatch_turn_number(
                        snapshot.turn_manager, is_opening_turn=is_opening_turn
                    )
                    _dispatch_package, _bank_result = await execute_intent_router_pre_narrator_pass(
                        intent_router=_intent_router,
                        snapshot=snapshot,
                        pack=sd.genre_pack,
                        action=action,
                        player_name=_acting_player_name,
                        additional_player_names=_additional_player_names or None,
                        dungeon_store=_dungeon_store,
                        palette=_dungeon_palette,
                        lookahead_handle=_lookahead_handle,
                        phase_timings=timings,
                        turn_number=_dispatch_turn_number,
                    )
                except IntentRouterFailure as exc:
                    if os.environ.get("SIDEQUEST_INTENT_ROUTER_DEGRADE_ON_FAIL"):
                        logger.warning(
                            "intent_router.degraded_continue genre=%s world=%s "
                            "player=%s action_len=%d reason=%s — env "
                            "SIDEQUEST_INTENT_ROUTER_DEGRADE_ON_FAIL set; "
                            "continuing turn with dispatch_package=None "
                            "(yesterday's narrator-only behavior). NOT a "
                            "silent fallback — operator opt-in.",
                            sd.genre_slug,
                            sd.world_slug,
                            _acting_player_name,
                            len(action),
                            exc,
                        )
                        # GM-panel coverage on the degrade path (Story 71-29):
                        # decompose() raised before reaching its own
                        # intent_router.decompose span, so the happy-path span
                        # never fired. Mirror it here with dispatch_count=0 and
                        # degraded=True so the routed intent_router.decompose
                        # state_transition event still reaches the live GM
                        # dashboard via WatcherSpanProcessor → hub.publish — the
                        # SAME broadcast path the happy path uses, no
                        # reimplementation. (This is a live-dashboard event, not
                        # a durable turn_telemetry row: span routing broadcasts
                        # through the hub, it does not call publish_event.)
                        # Without this the GM panel sees NO decompose event on a
                        # degraded turn and cannot tell a degrade from a turn
                        # where the spine never ran.
                        with intent_router_decompose_span(
                            action_length=len(action),
                            model=_INTENT_ROUTER_MODEL,
                        ) as _degrade_span:
                            _degrade_span.set_attribute("dispatch_count", 0)
                            _degrade_span.set_attribute("degraded", True)
                        _dispatch_package = None
                        _bank_result = None
                    else:
                        raise
                turn_context.dispatch_package = _dispatch_package
                turn_context.bank_result = _bank_result
                # The dispatch bank may have mutated snapshot.npcs; refresh so
                # build_narrator_prompt sees post-dispatch state.
                turn_context.npcs = list(snapshot.npcs)

                with orchestrator_process_action_span(action_len=len(action)):
                    result = await sd.orchestrator.run_narration_turn(
                        action, turn_context, room=self._room
                    )

                logger.info(
                    "session.narration_complete genre=%s world=%s degraded=%s duration_ms=%s",
                    sd.genre_slug,
                    sd.world_slug,
                    result.is_degraded,
                    result.agent_duration_ms,
                )

                # Capture encounter state BEFORE applying the narration result
                # so the dispatch below can detect live→resolved transitions.
                prior_encounter = snapshot.encounter
                prior_live = prior_encounter is not None and not prior_encounter.resolved
                prior_type = prior_encounter.encounter_type if prior_encounter else None

                # Playtest 2026-05-20: capture current_region pre-apply so the
                # region-mode LOCATION_DESCRIPTION branch can detect a true
                # change (region moves arrive via the current_region patch, not
                # the character-level result.location).
                prior_current_region = snapshot.current_region

                # Unified dispatch — passes the pack so encounter instantiation /
                # beat application / resolution happen in one place (emits the
                # Story-3.4 OTEL spans). ADR-074: read the dice outcome stashed
                # by the DICE_THROW handler and classify success/failure for
                # beat application; getattr so an absent field is a no-op.
                with timings.phase("state_apply"):
                    dice_outcome = getattr(sd, "pending_roll_outcome", None)
                    dice_failed: bool | None = None
                    if dice_outcome is not None:
                        outcome_name = getattr(dice_outcome, "name", None) or str(dice_outcome)
                        dice_failed = outcome_name in ("Fail", "CritFail")
                    dice_actor: str | None = getattr(sd, "pending_roll_actor", None)
                    opposed_player_d20: int | None = getattr(
                        sd,
                        "pending_opposed_player_d20",
                        None,
                    )
                    opposed_player_beat_id: str | None = getattr(
                        sd,
                        "pending_opposed_player_beat_id",
                        None,
                    )
                    if sd._room is None:
                        # Slug-connect always sets _room — programming error.
                        raise RuntimeError(
                            "_apply_narration_result_to_snapshot: sd._room "
                            "is None — slug-connect wiring missing"
                        )
                    # Story 45-30: capture pre-apply state so the render-trigger
                    # classifier can detect SCENE_CHANGE and ENCOUNTER_RESOLVED.
                    # Wave 2B (45-48): per-character — the acting PC's location.
                    _acting_for_render_trigger = _resolve_acting_character_name(sd, sd._room)
                    snapshot_location_before_apply = snapshot.party_location(
                        perspective=_acting_for_render_trigger
                    )
                    encounter_unresolved_before = (
                        snapshot.encounter is not None and not snapshot.encounter.resolved
                    )
                    # Spec 2026-05-20 step 7 — shared apply kwargs for the first
                    # apply and the reprompt-loop re-apply.
                    _apply_kwargs = dict(
                        room=sd._room,
                        pack=sd.genre_pack,
                        world=sd.world_slug,
                        dice_failed=dice_failed,
                        dice_actor=dice_actor,
                        opposed_player_d20=opposed_player_d20,
                        opposed_player_beat_id=opposed_player_beat_id,
                        opposed_player_actor=dice_actor,
                        acting_character_name=_resolve_acting_character_name(sd, sd._room),
                        # Story 83-1: thread the MonsterManual so creature mentions
                        # at the _apply_npc_mentions seam can look up real bestiary
                        # entries and embed them as creature_data on the pool member.
                        monster_manual=sd.monster_manual,
                    )
                    applied_outcome = _apply_narration_result_to_snapshot(
                        snapshot,
                        result,
                        sd.player_name,
                        **_apply_kwargs,
                    )

                    # Story 59-3 / ADR-113 — Intent Router lie-detector: compare
                    # what the router dispatched vs. what the engines engaged,
                    # emit one OTEL span per mismatch so the GM panel catches
                    # "convincing prose, zero mechanical backing". No-op while
                    # dispatch_package is None.
                    run_dispatch_engagement_watcher(
                        package=turn_context.dispatch_package,
                        snapshot=snapshot,
                    )

                    encounter_resolved_this_turn = encounter_unresolved_before and (
                        snapshot.encounter is None or snapshot.encounter.resolved
                    )
                    # Monster Manual lifecycle post-apply (ADR-059): mark dormant
                    # on scene-change FIRST so a location change clears Active
                    # anchors before scanning this turn's narration for new
                    # activations; save() last to persist the lifecycle.
                    if sd.monster_manual is not None:
                        post_apply_location = snapshot.party_location(
                            perspective=_acting_for_render_trigger
                        )
                        if (
                            snapshot_location_before_apply
                            and post_apply_location
                            and snapshot_location_before_apply != post_apply_location
                        ):
                            monster_manual_inject.mark_all_dormant(sd.monster_manual)
                        monster_manual_inject.mark_active_from_narration(
                            sd.monster_manual,
                            getattr(result, "narration", "") or "",
                            post_apply_location or "",
                        )
                        sd.monster_manual.save()
                    # Phase 5 (Story 47-3): drain magic-confrontation outbound
                    # queues here so the UI overlay + reveal panel surface in
                    # production gameplay, not just test harnesses.
                    self._dispatch_pending_magic_frames(snapshot)
                    # Consume the pending outcome — one turn per roll.
                    if dice_outcome is not None and hasattr(sd, "pending_roll_outcome"):
                        sd.pending_roll_outcome = None
                    if hasattr(sd, "pending_roll_actor"):
                        sd.pending_roll_actor = None
                    if hasattr(sd, "pending_opposed_player_d20"):
                        sd.pending_opposed_player_d20 = None
                    if hasattr(sd, "pending_opposed_player_beat_id"):
                        sd.pending_opposed_player_beat_id = None
                    # Task 14 — dogfight player-throw stash. When the sealed-letter
                    # branch yielded a PendingDogfightShot (player has a gun solution),
                    # stash it on sd AND broadcast a DiceRequest so the client throws
                    # the real Rapier d20. The stash survives until DICE_THROW reads
                    # and clears it — do NOT clear it here on the narration-turn path.
                    # (Contrast with opposed-check which is SET in dice_throw and READ
                    # here; dogfight is the mirror: SET here, READ in dice_throw.)
                    if applied_outcome.pending_dogfight_shot is not None:
                        _pending_df = applied_outcome.pending_dogfight_shot
                        sd.pending_dogfight_shot = _pending_df
                        # Build and broadcast the DiceRequest for the player's shot.
                        from uuid import uuid4  # noqa: PLC0415

                        from sidequest.protocol.dice import (  # noqa: PLC0415
                            DiceRequestPayload,
                            DieSides,
                            DieSpec,
                        )
                        from sidequest.protocol.messages import (  # noqa: PLC0415
                            DiceRequestMessage,
                        )
                        from sidequest.protocol.types import Stat  # noqa: PLC0415

                        _df_request_id = str(uuid4())
                        _df_request = DiceRequestPayload(
                            request_id=_df_request_id,
                            rolling_player_id=sd.player_id,
                            character_name=_pending_df.player_actor_name,
                            dice=[DieSpec(sides=DieSides.D20, count=1)],
                            modifier=_pending_df.player_modifier,
                            # SWN dogfight to-hit = Pilot skill + attack bonus
                            # (NOT a DEX ability check). The overlay stat badge
                            # is player-facing; label it what actually rolls so
                            # mechanics-first players read the right thing.
                            stat=Stat("PILOT"),
                            difficulty=_pending_df.player_target_number,
                            context="dogfight_player_gun_solution",
                        )
                        if sd._room is not None:
                            sd._room.broadcast(
                                DiceRequestMessage(payload=_df_request, player_id="server"),
                                exclude_socket_id=None,
                            )
                        _watcher_publish(
                            "state_transition",
                            {
                                "field": "dogfight",
                                "op": "player_dice_request_emitted",
                                "player_shooter_role": _pending_df.player_shooter_role,
                                "player_modifier": _pending_df.player_modifier,
                                "player_target_number": _pending_df.player_target_number,
                                "request_id": _df_request_id,
                            },
                            component="encounter",
                        )
                    # Story 45-5 / ADR-051: the opening narration is the round-1
                    # scene-set and bumps no counter; the first PLAYER_ACTION
                    # turn is the first real exchange.
                    if not is_opening_turn:
                        # Capture round before incrementing so the taunt-expiry
                        # span labels "the round that just ended" (Task 6).
                        _prior_round = snapshot.turn_manager.round
                        snapshot.turn_manager.record_interaction()

                        # Story 2026-05-10 — taunt decay tick (Task 6): enforce
                        # the 1-round taunt duration mechanically on every
                        # round-advance. No-op outside an active encounter.
                        if snapshot.encounter is not None and not snapshot.encounter.resolved:
                            from sidequest.game.taunt_tick import (  # noqa: PLC0415
                                tick_taunt_round_advance,
                            )

                            tick_taunt_round_advance(
                                snapshot.encounter,
                                prior_round=_prior_round,
                            )

                    # Story 45-19: arc-recompute tick (closes the world_history
                    # freeze bug). Consulted with the post-bump interaction so
                    # cadence boundaries align with the GM panel. Empty
                    # cached_history_chapters is a graceful no-op; the tick span
                    # still fires.
                    #
                    # Story 45-23: seed each newly-promoted chapter's narrative
                    # log + lore into the durable narrative_log + RAG lore store
                    # (closes the arc-content writeback gap). Per-chapter
                    # arc_embedding_seed span carries the seeded counts.
                    if should_recompute_arc(snapshot.turn_manager.interaction):
                        added_chapters = recompute_arc_history(snapshot, sd.cached_history_chapters)
                        if added_chapters:
                            from sidequest.game.lore_seeding import (  # noqa: PLC0415
                                seed_lore_from_arc_promotion,
                            )
                            from sidequest.telemetry.spans import (  # noqa: PLC0415
                                SPAN_WORLD_HISTORY_ARC_EMBEDDING_SEED,
                                Span,
                            )

                            for chapter in added_chapters:
                                # One seed-call per chapter so the OTEL span
                                # attributes the counts to the chapter id.
                                seed_result = seed_lore_from_arc_promotion(
                                    snapshot,
                                    sd.repository,
                                    sd.lore_store,
                                    [chapter],
                                )
                                with Span.open(
                                    SPAN_WORLD_HISTORY_ARC_EMBEDDING_SEED,
                                    {
                                        "chapter_id": getattr(chapter, "id", ""),
                                        "narrative_entries_appended": (
                                            seed_result.narrative_entries_appended
                                        ),
                                        "lore_fragments_minted": (
                                            seed_result.lore_fragments_minted
                                        ),
                                        "lore_fragments_skipped_duplicate": (
                                            seed_result.lore_fragments_skipped_duplicate
                                        ),
                                        "content_bytes_seeded": (seed_result.content_bytes_seeded),
                                        "interaction": (snapshot.turn_manager.interaction),
                                    },
                                ):
                                    pass

                    # Story 45-27: trope progression tick (advances passive
                    # progression, fires staggered beats, gates activations).
                    # Wired here so now_turn is post-bump and any engine-driven
                    # resolution is visible to the 45-20 handshake diff below.
                    from sidequest.game.trope_tick import tick_tropes  # noqa: PLC0415

                    tick_tropes(
                        snapshot,
                        sd.genre_pack,
                        now_turn=snapshot.turn_manager.interaction,
                        days_advanced=result.days_advanced,  # Story 50-4 — Pass A2 time skip
                    )

                    # Story 22-3: seed trope engine — ghost any active seed whose
                    # lifespan_turns elapsed. Same post-bump now_turn as tick_tropes.
                    from sidequest.game.seed_tick import tick_seeds  # noqa: PLC0415

                    tick_seeds(
                        snapshot,
                        sd.genre_pack,
                        now_turn=snapshot.turn_manager.interaction,
                    )

                    # Story 22-5: engagement-triggered seed injection. Draw a
                    # fresh seed when the player engaged a subsystem and fewer
                    # than 2 seeds are active. Same session_id for reproducibility.
                    if (
                        _dispatch_package is not None
                        and any(pd.dispatch for pd in _dispatch_package.per_player)
                        and len(snapshot.active_seeds) < 2
                        and getattr(sd.genre_pack, "seed_tropes", None)
                    ):
                        from sidequest.game.seed_tick import draw_engaged_seed  # noqa: PLC0415

                        draw_engaged_seed(
                            snapshot,
                            sd.genre_pack,
                            session_id=seed_session_id,
                            engagement_signal="dispatch",
                            now_turn=snapshot.turn_manager.interaction,
                        )

                    # Story 45-20: trope-resolution handshake. Diffs the baseline
                    # against the post-recompute snapshot to detect tropes that
                    # flipped to "resolved" this turn; writes the durable record
                    # (quest_log + active_stakes) and emits the handshake span.
                    # Idempotent re-detect emits active_stakes_appended=False.
                    _handshake_resolved_tropes(
                        snapshot,
                        trope_status_baseline,
                        player_name=sd.player_name,
                        source="chapter_promotion",
                    )

                    # Plan 6 Task 5 — complication-ledger resolution. Consumes
                    # the same resolved-trope diff the 45-20 handshake computed
                    # and calls resolve_complications_for_resolved_tropes →
                    # store.resolve_thread() for each matching open trope-thread.
                    # No-op until Plan 7 wires sd.dungeon_store (store-absent ⟺
                    # no open dungeon threads); the wiring tripwire is
                    # test_setpiece_attach_wiring.py, not a per-turn log.
                    _dungeon_store = getattr(sd, "dungeon_store", None)
                    if _dungeon_store is not None:
                        from sidequest.dungeon.setpiece_attach import (  # noqa: PLC0415
                            resolve_complications_for_resolved_tropes,
                        )

                        _resolved_this_turn = [
                            t.id
                            for t in snapshot.active_tropes
                            if t.status == "resolved"
                            and trope_status_baseline.get(t.id) != "resolved"
                        ]
                        resolve_complications_for_resolved_tropes(
                            resolved_trope_ids=_resolved_this_turn,
                            store=_dungeon_store,
                        )

                    now_encounter = snapshot.encounter
                    now_live = now_encounter is not None and not now_encounter.resolved

                    from sidequest.server.dispatch.encounter_lifecycle import (
                        _is_combat_category,
                        apply_level_ups,
                        apply_resource_patches,
                        award_turn_xp,
                    )

                    in_combat_now = (
                        snapshot.encounter is not None
                        and not snapshot.encounter.resolved
                        and _is_combat_category(sd.genre_pack, snapshot.encounter.encounter_type)
                    )
                    award_turn_xp(snapshot, in_combat=in_combat_now)
                    # ADR-021 track 1: milestone → level-up runs on the freshly
                    # awarded XP. The consumer that makes accumulation matter.
                    apply_level_ups(snapshot, sd.genre_pack.progression)

                    try:
                        crossed_thresholds = apply_resource_patches(
                            snapshot,
                            affinity_progress=result.affinity_progress or [],
                            lore_store=sd.lore_store,
                            turn=snapshot.turn_manager.interaction,
                        )
                    except Exception as exc:  # noqa: BLE001 — LLM typos must not kill the turn
                        logger.warning(
                            "resource.patch_failed error=%s — skipping threshold mint for this turn",
                            exc,
                        )
                        crossed_thresholds = []
                for t in crossed_thresholds:
                    logger.info(
                        "resource.threshold_crossed event_id=%s at=%s",
                        t.event_id,
                        t.at,
                    )

                with timings.phase("persistence"):
                    try:
                        # ADR-037: room owns the canonical snapshot, so room.save()
                        # suffices. Falls back to sd.repository.save on the legacy
                        # non-slug path.
                        if self._room is not None:
                            self._room.save()
                        else:
                            sd.repository.save(snapshot)
                        # Story 45-22: log the player's turn before the narrator
                        # response so the narrative_log shows both sources
                        # (pre-fix every entry was author='narrator'). Skipped on
                        # the opening turn (no real player input).
                        if not is_opening_turn:
                            acting_name = _resolve_acting_character_name(
                                sd,
                                self._room,
                            )
                            player_entry = NarrativeEntry(
                                timestamp=0,
                                round=snapshot.turn_manager.interaction,
                                author="player",
                                content=action,
                                tags=[],
                                speaker=acting_name,
                            )
                            sd.repository.append_narrative(player_entry)
                        narrative_entry = NarrativeEntry(
                            timestamp=0,
                            round=snapshot.turn_manager.interaction,
                            author="narrator",
                            content=result.narration,
                            tags=[],
                        )
                        sd.repository.append_narrative(narrative_entry)
                        logger.info(
                            "session.persisted turn=%d player=%s char_count=%d seat_count=%d",
                            snapshot.turn_manager.interaction,
                            sd.player_name,
                            len(snapshot.characters),
                            len(snapshot.player_seats),
                        )
                    except Exception as exc:
                        logger.error("session.persist_failed error=%s", exc)

                    # Story 75-15: lore write-through to Postgres (lore_fragments)
                    # so creation-seed + runtime-accreted fragments survive resume.
                    # The in-memory lore_store is NOT part of the snapshot, so the
                    # save above does not cover it. ISOLATED in its own try/except
                    # AFTER the snapshot+narrative writes: lore is the least-critical
                    # post-turn side-effect and must never suppress the durable
                    # narrative_log (75-1 review standard: wrap+log+OTEL-fail+continue).
                    try:
                        lore_written = sd.repository.save_lore_fragments(sd.lore_store)
                        logger.info(
                            "lore.persisted turn=%s fragments=%s",
                            snapshot.turn_manager.interaction,
                            lore_written,
                        )
                    except Exception as exc:  # noqa: BLE001 — lore persist must not crash the turn
                        logger.error("lore.persist_failed error=%s", exc)
                        _watcher_publish(
                            "lore_persist_failed",
                            {
                                "scope": "turn",
                                "turn": snapshot.turn_manager.interaction,
                                "error": str(exc),
                            },
                            component="rag",
                            severity="error",
                        )

                # Story 45-11 — turn_manager.round invariant lie-detector.
                # Emit on EVERY tick (invariant holding or not) so the GM panel
                # can tell "engaged + clean" from "not engaged". Read
                # MAX(round_number) from the durable narrative_log (ground truth);
                # the snapshot's in-memory mirror can drift from it.
                try:
                    max_narrative_round = int(sd.repository.max_narrative_round())
                except Exception as exc:  # noqa: BLE001 — telemetry must never crash a turn
                    logger.warning(
                        "round_invariant.max_lookup_failed error=%s",
                        exc,
                    )
                    max_narrative_round = 0
                with round_invariant_span(
                    round=snapshot.turn_manager.round,
                    interaction=snapshot.turn_manager.interaction,
                    max_narrative_round=max_narrative_round,
                ):
                    # Point-in-time emit; the helper sets the attributes.
                    pass

                # Story 75-1: accrete this turn's runtime-discovered KnownFacts
                # into the lore store BEFORE dispatching the embed worker, so the
                # freshly minted GameEvent fragments are in the pending queue and
                # get embedded this turn for next-turn RAG retrieval (restores the
                # Rust lore_sync accumulate-and-persist loop).
                self._accrete_lore_for_turn(sd)

                # Story 75-6: reproject this turn's mutated entities into the
                # universal-retrieval index BEFORE dispatching the embed worker,
                # so reprojected cards (embedding_pending=True) are in the queue
                # and get embedded this turn — keeping retrieval keyed on the
                # current cast (ADR-118 §D2 dirty-flag reproject).
                self._sync_entity_cards_for_turn(sd)

                # Story 37-33 / 75-6: embed pending lore fragments AND reprojected
                # entity cards in the background so the next turn's retrieval finds
                # them. Fire-and-forget — the turn returns immediately; embeds
                # populate during reading time.
                self._dispatch_embed_worker(sd)

                narration_text = result.narration or "(The world holds its breath...)"
                try:
                    narration_nbs = NonBlankString(narration_text)
                except Exception:
                    narration_nbs = NonBlankString("The world holds its breath...")

                # Forward extracted footnotes into the NarrationPayload so the UI
                # Knowledge journal fills (the session handler was the missing
                # link; useStateMirror already consumed them). Coerce raw dicts
                # to typed Footnotes, dropping any that fail validation.
                forwarded_footnotes: list[Footnote] = []
                fact_ids_minted_this_turn = 0
                for fn in result.footnotes or []:
                    if not isinstance(fn, dict):
                        continue
                    try:
                        footnote = Footnote(**fn)
                    except Exception as exc:  # noqa: BLE001 — drop-and-log is safer than a mid-turn crash
                        logger.warning(
                            "state.footnote_coerce_failed error=%s payload=%r",
                            exc,
                            fn,
                        )
                        continue
                    # ADR-100 Seam C: every Footnote reaching the UI MUST carry a
                    # stable fact_id, but narrators don't always supply one and
                    # the UI silently drops fact_id-less footnotes (sq-playtest
                    # 2026-05-15: 6 dropped in one turn). Mint a deterministic
                    # hash-based id so a later re-narration dedupes on the
                    # client's seenFactIds. Narrator-supplied fact_ids are left
                    # untouched — scenario clue_intake matches them to ClueNode.id.
                    if footnote.fact_id is None:
                        cat_str = (
                            footnote.category.value
                            if hasattr(footnote.category, "value")
                            else str(footnote.category)
                        )
                        digest_input = f"{footnote.summary}|{cat_str}|{footnote.is_new}"
                        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
                        footnote = footnote.model_copy(update={"fact_id": f"fn-{digest[:16]}"})
                        fact_ids_minted_this_turn += 1
                    forwarded_footnotes.append(footnote)
                if fact_ids_minted_this_turn > 0:
                    _watcher_publish(
                        "state.footnote_fact_id_minted",
                        {
                            "count": fact_ids_minted_this_turn,
                            "player_id": sd.player_id,
                            "turn_number": snapshot.turn_manager.interaction,
                            "reason": "narrator_omitted_fact_id",
                        },
                        component="footnotes",
                    )
                    logger.info(
                        "state.footnote_fact_id_minted count=%d player=%s turn=%d",
                        fact_ids_minted_this_turn,
                        sd.player_name,
                        snapshot.turn_manager.interaction,
                    )
                logger.info(
                    "state.footnotes_forwarded count=%d player=%s",
                    len(forwarded_footnotes),
                    sd.player_name,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "footnotes",
                        "count": len(forwarded_footnotes),
                        "player_id": sd.player_id,
                        "turn_number": snapshot.turn_manager.interaction,
                        "summaries": [fn.summary for fn in forwarded_footnotes][:10],
                    },
                    component="footnotes",
                )
                # Story 50-5 / ADR-100 seams A + B: feed footnotes to the
                # scenario clue graph. Matching fact_ids advance the scenario
                # and mint a Discovered KnownFact. No-op when no scenario/match.
                from sidequest.server.dispatch.scenario_clue_intake import (  # noqa: PLC0415
                    consume_clue_footnotes,
                )

                consume_clue_footnotes(
                    snapshot,
                    forwarded_footnotes,
                    active_character_name=snapshot.player_seats.get(sd.player_id, sd.player_name),
                )
                # Story 50-8 / ADR-053 AC-5: AccusationEvaluator dispatch
                # sibling. Imported so the evaluator is reachable on demand;
                # per-turn invocation is deferred until an accusation trigger
                # lands. The import keeps the module a live consumer, not dead code.
                from sidequest.server.dispatch.scenario_accusation import (  # noqa: F401, PLC0415
                    consume_accusation_request,
                )

                # Story 49-8: visibility classifier produces the v2 sidecar that
                # drives per-recipient routing + 2nd-person POV swap in
                # emitters.emit_event. Prefers result.action_rewrite.named
                # (ADR-039), else a first-sentence scan; returns atmospheric
                # (broadcasts canonical prose unchanged) when no PC name surfaces.
                from sidequest.server.visibility_classifier import (  # noqa: PLC0415
                    classify_narration_visibility,
                )

                _connected_player_ids = (
                    self._room.connected_player_ids()
                    if self._room is not None
                    and callable(getattr(self._room, "connected_player_ids", None))
                    else []
                )
                _player_id_to_character: dict[str, str] = {}
                if self._room is not None:
                    _slot_lookup = getattr(self._room, "slot_to_player_id", None)
                    if callable(_slot_lookup):
                        try:
                            _player_id_to_character = {
                                pid: slot for slot, pid in _slot_lookup().items()
                            }
                        except Exception:  # noqa: BLE001 — classifier must never crash a turn
                            _player_id_to_character = {}
                try:
                    _visibility_sidecar = classify_narration_visibility(
                        result=result,
                        snapshot=snapshot,
                        connected_player_ids=_connected_player_ids,
                        player_id_to_character=_player_id_to_character,
                    )
                except ValueError:
                    # Empty narration — classifier fails loud; emit the canonical
                    # prose unchanged so a degraded turn still surfaces to the UI.
                    _visibility_sidecar = None

                narration_payload = NarrationPayload(
                    text=narration_nbs,
                    state_delta=None,
                    footnotes=forwarded_footnotes,
                    visibility_sidecar=_visibility_sidecar,
                )
                # MP-03 Task 3: route through EventLog + ProjectionFilter.
                # ADR-105 Track A: a merged-MP narration (>1 connected) has no
                # sole author, so thread the driver's player_id as
                # author_player_id to get it projected/POV-swapped like any
                # recipient (not the solo raw bypass — a firewall+POV breach).
                # Solo (<=1) passes None and keeps the raw-bypass invariant.
                _mp_author = (
                    self._session_data.player_id
                    if self._session_data is not None and len(_connected_player_ids) > 1
                    else None
                )
                with timings.phase("broadcast"):
                    narration_msg = self._emit_event(
                        "NARRATION", narration_payload, author_player_id=_mp_author
                    )

                    # ADR-105 B3: emit each private prose segment as its own
                    # NARRATION_SEGMENT, routed by visible_to to the owning PC.
                    # The CoreInvariant (B1) firewalls non-recipients;
                    # author_player_id (Track A) gives the owner a real
                    # per-recipient projection pass. Zero segments on public turns.
                    _private_segments = getattr(result, "private_prose_segments", []) or []
                    if _private_segments:
                        _seat_to_player = {
                            name: pid for pid, name in snapshot.player_seats.items() if name
                        }
                        _seg_turn_id = (
                            f"{sd.genre_slug}:{sd.world_slug}:"
                            f"{sd.player_id}:{snapshot.turn_manager.interaction}"
                        )
                        for _seg in _private_segments:
                            _seg_text = (_seg.get("text") or "").strip()
                            if not _seg_text:
                                continue
                            _seg_anchor = (_seg.get("anchor_pc") or "").strip() or None
                            _owner_pid = _seat_to_player.get(_seg_anchor) if _seg_anchor else None
                            if _owner_pid is None:
                                # Fail loud, never leak: an unresolvable owner
                                # means we can't safely route this prose. Drop it
                                # (routing-to-all would be the ADR-105 breach)
                                # and surface it for the GM panel.
                                logger.warning(
                                    "narration.segment_unroutable "
                                    "anchor_pc=%r seated=%r — DROPPED (no leak)",
                                    _seg_anchor,
                                    list(_seat_to_player),
                                )
                                _watcher_publish(
                                    "state_transition",
                                    {
                                        "field": "narration.segment_routed",
                                        "anchor_pc": _seg_anchor or "",
                                        "visible_to": "",
                                        "recipient_count": 0,
                                        "withheld_from_count": len(_connected_player_ids),
                                        "routed": False,
                                    },
                                    component="projection",
                                    severity="warning",
                                )
                                continue
                            # author_player_id=_owner_pid makes the owner the
                            # emitter: peers are excluded at fan-out by the
                            # CoreInvariant (B1); the owner's frame is projected,
                            # POV-swapped (B4), and returned. Since emit_event
                            # never delivers to the emitter, the owner's own
                            # segment is pushed to the owner's current socket below.
                            _seg_msg = self._emit_event(
                                "NARRATION_SEGMENT",
                                NarrationSegmentPayload(
                                    text=_seg_text,
                                    anchor_pc=_seg_anchor,
                                    turn_id=_seg_turn_id,
                                    # ADR-105 B4: per-segment POV. Each segment
                                    # is single-PC, so anchor_pc + pc_anchored
                                    # let _apply_pov_swap rewrite it to 2nd-person
                                    # for the owner.
                                    visibility_sidecar={
                                        "visible_to": [_owner_pid],
                                        "fidelity": {},
                                        "anchor_pc": _seg_anchor,
                                        "pov_strategy": "pc_anchored",
                                    },
                                ),
                                author_player_id=_owner_pid,
                            )
                            # Deliver the owner's projected+swapped frame to the
                            # owner's live socket; lookup at delivery time so a
                            # reconnected owner's new socket gets it. On the
                            # EventLog regardless — a missing socket is a deferred
                            # read, not a leak.
                            _seg_room = self._room
                            if _seg_msg is not None and _seg_room is not None:
                                _seg_sock_fn = getattr(_seg_room, "socket_for_player", None)
                                _seg_q_fn = getattr(_seg_room, "queue_for_socket", None)
                                if callable(_seg_sock_fn) and callable(_seg_q_fn):
                                    _seg_sock = _seg_sock_fn(_owner_pid)
                                    _seg_q = _seg_q_fn(_seg_sock) if _seg_sock is not None else None
                                    if _seg_q is not None:
                                        _seg_q.put_nowait(_seg_msg)
                            _watcher_publish(
                                "state_transition",
                                {
                                    "field": "narration.segment_routed",
                                    "anchor_pc": _seg_anchor,
                                    "visible_to": _owner_pid,
                                    "recipient_count": 1,
                                    "withheld_from_count": max(len(_connected_player_ids) - 1, 0),
                                    "routed": True,
                                },
                                component="projection",
                            )

                # Pingpong 2026-04-26 [S3-REGRESSION]: emit a SCRAPBOOK_ENTRY every
                # narration turn so the UI gallery has metadata to merge with the
                # later IMAGE. Fields come from the result + stamped snapshot.
                with timings.phase("dispatch_post"):
                    # Story 45-31: consult the daemon-state mirror so the
                    # dispatcher below can skip its round-trip when the daemon is
                    # UNRESPONSIVE (sd.render_unavailable_pending is the flag).
                    from sidequest.daemon_client.state_mirror import (
                        get_mirror as _get_mirror,
                    )

                    _hb_mirror = _get_mirror()
                    sd.render_unavailable_pending = (
                        _hb_mirror.last_heartbeat_ts() is not None and _hb_mirror.is_unresponsive()
                    )

                    # Story 45-30: classify the render trigger once so the same
                    # value lands in both the SCRAPBOOK_ENTRY render_status and
                    # the dispatcher below (classify_trigger is pure).
                    from sidequest.server.render_trigger import (
                        RenderTriggerReason,
                        classify_trigger,
                    )

                    _trigger_reason = classify_trigger(
                        result,
                        snapshot_location_before=snapshot_location_before_apply,
                        encounter_resolved_this_turn=encounter_resolved_this_turn,
                    )

                    # Unified render_status (45-30 + 45-31): daemon-unavailable
                    # wins over policy (no render either way), then policy decides
                    # skipped_policy vs rendered.
                    if sd.render_unavailable_pending:
                        _render_status = "unavailable"
                    elif _trigger_reason is RenderTriggerReason.NONE_POLICY:
                        _render_status = "skipped_policy"
                    else:
                        _render_status = "rendered"

                    try:
                        self._emit_scrapbook_entry(
                            sd=sd,
                            snapshot=snapshot,
                            result=result,
                            render_status=_render_status,
                        )
                    except Exception as exc:  # noqa: BLE001 — scrapbook must never crash a turn
                        logger.warning(
                            "scrapbook.emit_failed turn=%d error=%s",
                            snapshot.turn_manager.interaction,
                            exc,
                        )

                    # Group G Task 6: reify prompt-redacted dispatches (parked on
                    # result.secret_routes) as SECRET_NOTE events so the
                    # ProjectionFilter delivers each only to its visible_to
                    # recipients. See build_secret_note_events for skip rules.
                    if result.secret_routes:
                        for _envelope in build_secret_note_events(
                            result.secret_routes,
                            turn_id=f"{sd.genre_slug}:{sd.world_slug}:{sd.player_id}:{snapshot.turn_manager.interaction}",
                        ):
                            import json as _json

                            _payload_data = _json.loads(_envelope.payload_json)
                            self._emit_event(
                                "SECRET_NOTE",
                                SecretNotePayload(
                                    turn_id=_payload_data["turn_id"],
                                    idempotency_key=_payload_data["idempotency_key"],
                                    subsystem=_payload_data["subsystem"],
                                    params=_payload_data.get("params", {}),
                                    visibility_sidecar=_payload_data["_visibility"],
                                ),
                            )

                    # Story 3.4 Task 11: emit CONFRONTATION on encounter state
                    # transition, with a span event for the GM panel.
                    # Pingpong 2026-04-26 S2-BUG: confrontations were private to the
                    # actor (peers froze) because the message was only appended to
                    # the actor's outbound, never broadcast. Fix: route through
                    # _emit_event so EventLog + ProjectionFilter fan it out per-peer.
                    confrontation_msg: object | None = None
                    confrontation_payload: ConfrontationPayload | None = None
                    confrontation_event_attrs: dict[str, object] | None = None
                    cdef = None
                    if now_live and now_encounter is not None:
                        from sidequest.server.dispatch.confrontation import (
                            build_confrontation_payload,
                            find_confrontation_def,
                            make_confrontation_portrait_resolver,
                        )

                        cdef = find_confrontation_def(
                            sd.genre_pack.rules.confrontations if sd.genre_pack.rules else [],
                            now_encounter.encounter_type,
                        )
                        # No silent fallback: an active encounter type missing
                        # from the pack is a pack-data bug — raise (matches beat-apply).
                        if cdef is None:
                            raise ValueError(
                                f"active encounter type {now_encounter.encounter_type!r} "
                                f"not in pack confrontations (genre={sd.genre_slug!r})"
                            )
                        # Canonical full-union payload — persisted to EventLog
                        # below for replay parity (and delivered to stub-room test
                        # fixtures). recipient_pc=None: not projected per-PC; the
                        # overlay loop below class-filters per socket (Story 49-7).
                        payload_dict = build_confrontation_payload(
                            encounter=now_encounter,
                            cdef=cdef,
                            genre_slug=sd.genre_slug,
                            recipient_pc=None,
                            core_resolver=sd.snapshot.find_creature_core,
                            # Story 85-3: stakes + opponent portrait on the
                            # canonical union (persisted + delivered to stub-room
                            # fixtures). The supplier below re-projects per socket.
                            active_stakes=sd.snapshot.active_stakes,
                            portrait_resolver=make_confrontation_portrait_resolver(
                                snapshot=sd.snapshot,
                                genre_pack=sd.genre_pack,
                                genre_slug=sd.genre_slug,
                            ),
                        )
                        confrontation_payload = ConfrontationPayload(**payload_dict)
                        confrontation_event_attrs = {
                            "active": True,
                            "encounter_type": now_encounter.encounter_type,
                            "genre_slug": sd.genre_slug,
                        }
                    elif prior_live and not now_live:
                        from sidequest.server.dispatch.confrontation import (
                            build_clear_confrontation_payload,
                        )

                        assert prior_type is not None  # guaranteed by prior_live=True
                        payload_dict = build_clear_confrontation_payload(
                            encounter_type=prior_type,
                            genre_slug=sd.genre_slug,
                        )
                        confrontation_payload = ConfrontationPayload(**payload_dict)
                        confrontation_event_attrs = {
                            "active": False,
                            "encounter_type": prior_type,
                            "genre_slug": sd.genre_slug,
                        }

                    if confrontation_payload is not None:
                        # Story 59-16: ONE filtered delivery path. On the LIVE
                        # branch, deliver a single class-filtered CONFRONTATION to
                        # every connected socket (including the emitter) via a
                        # per-recipient supplier; the canonical full-union payload
                        # is persisted to the EventLog ONLY (replay/audit) and is
                        # never sent to a client socket. This replaces the Story
                        # 49-7 union-broadcast + per-PC overlay race (the UI's
                        # last-message-wins reverted the tab to the 16-button union
                        # after a flee/reconnect). A seated, connected PC whose
                        # class will not resolve fails LOUD (ERROR span) and gets
                        # no frame — never the union; an unseated/lobby socket gets
                        # nothing silently. The clear branch (empty beats) stays a
                        # single plain emit — there is nothing to per-PC filter.
                        if now_live and now_encounter is not None:
                            from sidequest.server.dispatch.confrontation import (
                                make_confrontation_frame_supplier,
                            )

                            assert cdef is not None  # set above; the cdef-is-None case raised
                            # Story 59-20: shared single-filtered-delivery supplier
                            # (same seam the dice mid-turn + resume paths use).
                            _confrontation_frame_for = make_confrontation_frame_supplier(
                                snapshot=sd.snapshot,
                                genre_pack=sd.genre_pack,
                                encounter=now_encounter,
                                cdef=cdef,
                                genre_slug=sd.genre_slug,
                            )

                            with encounter_momentum_broadcast_span(
                                encounter_type=now_encounter.encounter_type,
                                player_metric_after=now_encounter.player_metric.current,
                                opponent_metric_after=now_encounter.opponent_metric.current,
                                source="narration_apply",
                                beat_id=None,
                            ):
                                confrontation_msg = self._emit_event(
                                    "CONFRONTATION",
                                    confrontation_payload,
                                    per_recipient_payload=_confrontation_frame_for,
                                )
                        else:
                            # Clear (overlay unmount): empty beats, nothing to
                            # per-PC filter — deliver the same frame to every
                            # connected socket via the single path so the
                            # dispatcher's tab unmounts too.
                            _clear_payload = confrontation_payload
                            confrontation_msg = self._emit_event(
                                "CONFRONTATION",
                                confrontation_payload,
                                per_recipient_payload=lambda _pid: _clear_payload,
                            )

                        assert confrontation_event_attrs is not None
                        trace.get_current_span().add_event(
                            "confrontation.dispatched",
                            confrontation_event_attrs,
                        )
                        # OTEL lie-detector (CLAUDE.md OTEL principle): evidence the
                        # broadcast reached peers, not just the actor — without it a
                        # regression to actor-only delivery is invisible.
                        peer_player_ids: list[str] = []
                        room_slug: str = ""
                        if self._room is not None:
                            # Some test fixtures use a minimal Room shim lacking
                            # connected_player_ids / slug. The OTEL hook is
                            # best-effort — never crash the turn for it.
                            import contextlib  # noqa: PLC0415 — local import keeps hot path lean

                            with contextlib.suppress(AttributeError):
                                peer_player_ids = [
                                    pid
                                    for pid in self._room.connected_player_ids()
                                    if pid != sd.player_id
                                ]
                            room_slug = getattr(self._room, "slug", "") or ""
                        logger.info(
                            "confrontation.peer_projection_broadcast slug=%s acting=%s "
                            "encounter_type=%s active=%s peers=%s",
                            room_slug,
                            sd.player_id,
                            confrontation_event_attrs["encounter_type"],
                            confrontation_event_attrs["active"],
                            peer_player_ids,
                        )
                        _watcher_publish(
                            "confrontation_peer_projection_broadcast",
                            {
                                "slug": room_slug,
                                "acting_player_id": sd.player_id,
                                "encounter_type": confrontation_event_attrs["encounter_type"],
                                "active": confrontation_event_attrs["active"],
                                "peers": peer_player_ids,
                            },
                            component="confrontation",
                        )

                        # sq-playtest 2026-05-12 lie-detector: compare narration
                        # kill-claims against the engine's encounter state (Chalk
                        # Moth "kill turn": prose killed the moth but no resolution
                        # fired). Surfaces the NARRATOR side so the GM panel can
                        # see when prose outruns the dial.
                        from sidequest.server.confrontation_lifecycle_detector import (
                            build_lifecycle_snapshot,
                        )

                        lifecycle_snapshot = build_lifecycle_snapshot(
                            narration=narration_text,
                            encounter_active_pre_apply=prior_live,
                            encounter=snapshot.encounter,
                            encounter_resolved_this_turn=encounter_resolved_this_turn,
                        )
                        _watcher_publish(
                            "confrontation_lifecycle",
                            {
                                "slug": room_slug,
                                "acting_player_id": sd.player_id,
                                **lifecycle_snapshot.to_watcher_attrs(),
                            },
                            component="confrontation",
                        )

                with timings.phase("broadcast"):
                    # MP merged-dispatch: the four shared-world envelopes
                    # (NARRATION_END / CHAPTER_MARKER / PARTY_STATUS / AUDIO_CUE)
                    # are NOT durable (unlike NARRATION on the EventLog path).
                    # Pingpong 2026-04-30 caught both halves of a delivery bug:
                    # peers missed them (outbound went to the dispatcher socket
                    # alone), and the dispatcher's own reconnected socket missed
                    # them on a mid-await refresh. Fix: broadcast to every CURRENT
                    # socket (exclude_socket_id=None) — single delivery path, picks
                    # up reconnected sockets, no double-send. Legacy non-slug path
                    # (self._room is None) falls back to outbound.append.
                    _has_room = self._room is not None

                    # OTEL lie-detector: one watcher event per shared-world frame
                    # recording recipient socket_ids + player_ids, so the GM panel
                    # can verify every socket received it (catches regressions).
                    def _emit_shared_world_frame(msg: object, frame_kind: str) -> None:
                        if not _has_room:
                            outbound.append(msg)
                            return
                        room = self._room
                        assert room is not None  # noqa: S101 — narrowed by _has_room
                        room.broadcast(msg, exclude_socket_id=None)
                        # OTEL lie-detector keyed by recipient player_ids (catches
                        # the last-submitter-stuck regression). Wrapped: the
                        # broadcast above is load-bearing, OTEL must never crash a turn.
                        try:
                            recipients_method = getattr(room, "connected_player_ids", None)
                            recipient_player_ids = (
                                recipients_method() if callable(recipients_method) else []
                            )
                            slug_attr = getattr(room, "slug", "")
                            _watcher_publish(
                                "shared_world_frame_broadcast",
                                {
                                    "frame_kind": frame_kind,
                                    "slug": slug_attr,
                                    "recipient_count": len(recipient_player_ids),
                                    "recipient_player_ids": recipient_player_ids,
                                    "dispatcher_player_id": sd.player_id,
                                },
                                component="multiplayer",
                            )
                        except Exception as exc:  # noqa: BLE001 — telemetry must never crash a turn
                            logger.warning(
                                "shared_world_frame.watcher_publish_failed kind=%s error=%s",
                                frame_kind,
                                exc,
                            )

                    outbound: list[object] = [narration_msg]
                    if confrontation_msg is not None:
                        # Story 59-16: emit_event already delivered the single
                        # filtered CONFRONTATION to every connected socket —
                        # including the dispatcher's CURRENT socket, looked up at
                        # delivery time so a mid-await reconnect is covered. No
                        # second push, and the canonical union never reaches a
                        # socket. Legacy / stub rooms (no EventLog or no
                        # connected_player_ids) get the emitter's filtered frame
                        # appended to outbound so the return value carries it.
                        connected_player_ids_fn = (
                            getattr(self._room, "connected_player_ids", None) if _has_room else None
                        )
                        if (
                            _has_room
                            and self._event_log is not None
                            and callable(connected_player_ids_fn)
                        ):
                            room = self._room
                            assert room is not None  # noqa: S101 — narrowed above
                            # OTEL lie-detector: how many sockets the single
                            # filtered fan-out reached (catches a future skip).
                            try:
                                slug_attr = getattr(room, "slug", "")
                                connected = list(room.connected_player_ids())
                                _watcher_publish(
                                    "shared_world_frame_broadcast",
                                    {
                                        "frame_kind": "confrontation_projection",
                                        "slug": slug_attr,
                                        "recipient_count": len(connected),
                                        "recipient_player_ids": connected,
                                        "dispatcher_player_id": sd.player_id,
                                        "dispatcher_delivery_path": "single_filtered",
                                    },
                                    component="multiplayer",
                                )
                            except Exception as exc:  # noqa: BLE001 — telemetry must never crash a turn
                                logger.warning(
                                    "shared_world_frame.watcher_publish_failed "
                                    "kind=confrontation_projection error=%s",
                                    exc,
                                )
                        else:
                            # Legacy / stub-room fallback: append the emitter's
                            # filtered frame to outbound so the return value carries it.
                            outbound.append(confrontation_msg)
                    # CHAPTER_MARKER — drives the UI's useRunningHeader title. Emit
                    # one frame per location change so the header tracks narration
                    # (Pingpong 2026-04-24 "location not rendered on resume"; the
                    # slug-resume bootstrap emits the symmetric frame).
                    if result.location:
                        chapter_marker_msg = ChapterMarkerMessage(
                            payload=ChapterMarkerPayload(
                                title=None,
                                location=_resolve_location_display(
                                    sd.genre_pack,
                                    sd.world_slug,
                                    snapshot.party_location(perspective=_acting_for_render_trigger),
                                ),
                            ),
                            player_id=sd.player_id,
                        )
                        _emit_shared_world_frame(chapter_marker_msg, "CHAPTER_MARKER")
                        # ADR-096 Task 20b: emit TACTICAL_GRID for room_graph
                        # worlds whose new location has a room YAML on disk.
                        _maybe_emit_tactical_grid(
                            self,
                            sd=sd,
                            snapshot=snapshot,
                            actor=_acting_for_render_trigger,
                            emit_fn=_emit_shared_world_frame,
                        )
                        # Story 54-2 / ADR-109: emit LOCATION_DESCRIPTION on the
                        # same room-change branch — typed entity manifest + base
                        # prose to keep the UI Location tab in sync.
                        _maybe_emit_location_description(
                            self,
                            sd=sd,
                            snapshot=snapshot,
                            actor=_acting_for_render_trigger,
                            emit_fn=_emit_shared_world_frame,
                        )
                    # Playtest 2026-05-20 — per-turn LOCATION_DESCRIPTION on region
                    # change for region-mode worlds (the result.location branch
                    # above is room_graph-level). The region patch is the only
                    # signal here. No-op for non-region worlds + unchanged regions.
                    _world_for_region_emit = sd.genre_pack.worlds.get(sd.world_slug)
                    _is_region_mode_world = (
                        _world_for_region_emit is not None
                        and _world_for_region_emit.cartography.navigation_mode
                        == NavigationMode.region
                    )
                    if _is_region_mode_world:
                        # Lie-detector: on EVERY region-mode turn record whether
                        # the narrator declared/changed current_region. "Prose
                        # moved the party but current_region didn't" is the
                        # frozen-Location-panel failure (playtest 2026-05-21);
                        # firing every turn keeps a regression visible.
                        _region_changed = bool(
                            snapshot.current_region
                            and snapshot.current_region != prior_current_region
                        )
                        _watcher_publish(
                            "narrator.region_patch_check",
                            {
                                "genre": sd.genre_slug,
                                "world": sd.world_slug,
                                "current_region": snapshot.current_region or "",
                                "prior_current_region": prior_current_region or "",
                                "current_region_present": bool(snapshot.current_region),
                                "region_changed": _region_changed,
                            },
                            component="location",
                        )
                        if _region_changed:
                            _maybe_emit_location_description(
                                self,
                                sd=sd,
                                snapshot=snapshot,
                                actor=None,
                                emit_fn=_emit_shared_world_frame,
                                room_id_override=snapshot.current_region,
                            )
                    # Beneath Sünden seam 3: project the live region graph to the
                    # UI Map tab every turn (NOT gated on result.location — covers
                    # turn 1 + resume, curing "No map data yet"). No-op off
                    # beneath_sunden.
                    _maybe_emit_dungeon_map(
                        self,
                        sd=sd,
                        snapshot=snapshot,
                        emit_fn=_emit_shared_world_frame,
                    )
                    # ADR-136: relationship roster rides the same per-turn /
                    # resume cadence as the dungeon-map projection above — NOT
                    # gated on a location/region change, because a disposition
                    # shift (the common case) happens mid-scene without moving
                    # the party. The emitter is internally change-gated on the
                    # roster signature, so an unchanged roster is a no-op and a
                    # resume re-fires a fresh roster on the next turn. Transient
                    # broadcast (_emit_shared_world_frame), never event-sourced.
                    _maybe_emit_relationships(
                        self,
                        snapshot=snapshot,
                        emit_fn=_emit_shared_world_frame,
                    )
                    # ADR-137 / Story 77-8: quest spine roster rides the same
                    # per-turn / resume cadence as the relationship roster above.
                    # Internally change-gated on the spine signature (quests +
                    # anchors + stakes), so an unchanged spine is a no-op and an
                    # empty spine shows nothing. Transient broadcast
                    # (_emit_shared_world_frame), never event-sourced — the UI
                    # quest/objective panel (Story 77-5) consumes it.
                    _maybe_emit_quests(
                        self,
                        snapshot=snapshot,
                        emit_fn=_emit_shared_world_frame,
                    )
                    # Region-mode cartography map: project the region graph to
                    # the UI Map tab EVERY turn (NOT gated on a region change —
                    # covers turn 1 + intra-region moves + resume, curing "No map
                    # data yet"; EH-2 burning_peace 2026-06-05). Idempotent (the
                    # UI replaces its MapState); no-op for room_graph worlds (they
                    # use DUNGEON_MAP). Mirrors the dungeon-map / relationships /
                    # quests projections above, which already fire unconditionally.
                    _maybe_emit_cartography_map(
                        self,
                        sd=sd,
                        snapshot=snapshot,
                        emit_fn=_emit_shared_world_frame,
                        acting_perspective=_acting_for_render_trigger,
                    )
                    # Story 54-7 / ADR-109: encounter overlay transitions —
                    # activate when a fresh encounter with a location_overlay goes
                    # live, deactivate when one resolves. Decoupled from room change.
                    if now_live and not prior_live and now_encounter is not None:
                        _maybe_emit_location_overlay_changed(
                            self,
                            sd=sd,
                            snapshot=snapshot,
                            transition="activate",
                            emit_fn=_emit_shared_world_frame,
                        )
                    if (
                        encounter_resolved_this_turn
                        and prior_encounter is not None
                        and prior_encounter.location_overlay is not None
                    ):
                        _maybe_emit_location_overlay_changed(
                            self,
                            sd=sd,
                            snapshot=snapshot,
                            transition="deactivate",
                            emit_fn=_emit_shared_world_frame,
                            prior_overlay=prior_encounter.location_overlay,
                        )
                    # Story 45-1 — sealed-letter shared-world handshake: ride the
                    # canonical post-resolution delta on NARRATION_END so peers see
                    # ground-truth location/encounter/party (stops the narrator
                    # fabricating geography between PCs).
                    handshake_delta = build_shared_world_delta(
                        snapshot,
                        room=self._room,
                    )
                    # Magic Phase 4: ride post-resolution magic_state on the
                    # NARRATION_END handshake every turn (gating would silently
                    # desync the ledger after a reconnect).
                    magic_state_dict = (
                        snapshot.magic_state.model_dump(mode="json")
                        if snapshot.magic_state is not None
                        else None
                    )
                    narration_end_msg = NarrationEndMessage(
                        type="NARRATION_END",  # type: ignore[arg-type]
                        payload=NarrationEndPayload(
                            state_delta=_shared_world_delta_to_state_delta(
                                handshake_delta,
                                magic_state=magic_state_dict,
                            ),
                        ),
                        player_id=sd.player_id,
                    )
                    _emit_shared_world_frame(narration_end_msg, "NARRATION_END")

                    # MP turn-ownership clear (ADR-036): pairs with the
                    # TURN_STATUS{active} broadcast at action receipt — peers' banner
                    # stays stuck without it. Broadcast to every socket; the UI
                    # clears activePlayerName on status="resolved".
                    if self._room is not None and sd.player_name:
                        try:
                            acting_name = _resolve_acting_character_name(sd, self._room)
                            turn_resolved_msg = TurnStatusMessage(
                                payload=TurnStatusPayload(
                                    player_name=NonBlankString(acting_name),
                                    status="resolved",
                                    # Explicit empty roster — the UI clears
                                    # turnStatusEntries on resolved; `[]` prevents
                                    # App.tsx re-pushing a stale "pending" entry.
                                    entries=[],
                                ),
                                player_id=sd.player_id or "",
                            )
                            self._room.broadcast(turn_resolved_msg, exclude_socket_id=None)
                            logger.info(
                                "session.turn_status_resolved player=%s player_id=%s slug=%s",
                                acting_name,
                                sd.player_id,
                                self._room.slug,
                            )
                            _watcher_publish(
                                "turn_status",
                                {
                                    "status": "resolved",
                                    "player_name": acting_name,
                                    "player_id": sd.player_id,
                                    "slug": self._room.slug,
                                },
                                component="session",
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "session.turn_status_resolved_broadcast_failed error=%s",
                                exc,
                            )

                    # Refresh PARTY_STATUS so current_location + any HP/inventory
                    # mutations propagate to the client header / CharacterSheet
                    # (pre-fix it fired once at chargen-end and froze the location;
                    # playtest 2026-04-22).
                    if snapshot.characters:
                        try:
                            # MP: resolve "self" by sd.player_id, not
                            # characters[0] — passing characters[0] mis-tags the
                            # wrong PC as "(YOU)" for any non-first player
                            # (playtest 2026-04-25 "Tab 2 sees Laverne (YOU)").
                            self_char = (
                                views.resolve_self_character(self, sd) or snapshot.characters[0]
                            )
                            party_status = views.build_session_start_party_status(
                                self, sd, self_char, sd.player_id
                            )
                            # MP merged-dispatch: broadcast the party refresh to
                            # every socket. Safe as-is — each peer's UI resolves
                            # "(YOU)" via the seat_map-tagged player_id, not the
                            # dispatcher's. Single broadcast covers both peers and
                            # the dispatcher's reconnected socket (pingpong 2026-04-30).
                            _emit_shared_world_frame(party_status, "PARTY_STATUS")
                            # Wave 2B (45-48): log the actor's own scene — there's
                            # no party-frame snapshot.location anymore.
                            _ps_log_loc = (
                                snapshot.party_location(perspective=self_char.core.name) or ""
                            )
                            logger.info(
                                "state.party_status_emitted reason=turn_end location=%r turn=%d "
                                "self_char=%s",
                                _ps_log_loc,
                                snapshot.turn_manager.interaction,
                                self_char.core.name,
                            )
                            _watcher_publish(
                                "state_transition",
                                {
                                    "field": "party_status",
                                    "reason": "turn_end",
                                    "location": _ps_log_loc,
                                    "turn_number": snapshot.turn_manager.interaction,
                                    "player_id": sd.player_id,
                                },
                                component="party_status",
                            )
                        except Exception as exc:  # noqa: BLE001 — party refresh must never crash a turn
                            logger.warning("state.party_status_refresh_failed error=%s", exc)

                # Visual-scene render dispatch. Fire-and-forget: RENDER_QUEUED
                # ships now; the async task posts IMAGE when the daemon replies.
                # Short-circuits on render flag off / no scene / no daemon / no queue.
                render_queued = self._maybe_dispatch_render(
                    sd,
                    result,
                    encounter_resolved_this_turn=encounter_resolved_this_turn,
                    snapshot_location_before=snapshot_location_before_apply,
                    acting_character_name=_acting_for_render_trigger,
                )
                if render_queued is not None:
                    outbound.append(render_queued)

                # Audio DJ dispatch. Synchronous (local filesystem lookup):
                # AUDIO_CUE ships with this turn's frames. Broadcast so the music
                # bed transitions in lock-step for every player.
                audio_cue = self._maybe_dispatch_audio(sd, result)
                if audio_cue is not None:
                    _emit_shared_world_frame(audio_cue, "AUDIO_CUE")

                # turn_complete is emitted by the validator (ADR-089 §6.7); the
                # TurnRecord below is the single source of truth.

                # --- TurnRecord assembly + validator submit ---
                # The validator must NEVER crash the hot path.
                if self._validator is not None:
                    try:
                        _patch_summaries: list[PatchSummary] = []
                        if result.location:
                            _patch_summaries.append(
                                PatchSummary(patch_type="location", fields_changed=["location"])
                            )
                        # Story 77-4: the legacy quest_updates lane was retired
                        # (record_quest is the typed home); the NarrationTurnResult
                        # field is gone, so there is no quest PatchSummary here.
                        if result.lore_established:
                            _patch_summaries.append(
                                PatchSummary(patch_type="lore", fields_changed=["lore_established"])
                            )
                        if result.npcs_present:
                            _patch_summaries.append(
                                PatchSummary(
                                    patch_type="npc_pool",
                                    fields_changed=[n.name for n in result.npcs_present],
                                )
                            )
                        if result.items_gained or result.items_lost:
                            _patch_summaries.append(
                                PatchSummary(patch_type="inventory", fields_changed=[])
                            )

                        _beats_fired: list[tuple[str, float]] = []
                        for beat in result.beat_selections or []:
                            _beats_fired.append(
                                (
                                    getattr(beat, "trope_id", None)
                                    or getattr(beat, "beat_id", None)
                                    or "unknown",
                                    float(getattr(beat, "threshold", 0.0) or 0.0),
                                )
                            )

                        timings.mark_done()
                        record = TurnRecord(
                            turn_id=snapshot.turn_manager.interaction,
                            timestamp=datetime.now(UTC),
                            player_id=sd.player_id,
                            player_input=action,
                            classified_intent=applied_outcome.classified_intent,
                            agent_name=result.agent_name or "narrator",
                            narration=result.narration or "",
                            patches_applied=_patch_summaries,
                            snapshot_before_hash=snapshot_before_hash,
                            snapshot_after=snapshot,
                            delta=None,  # TODO: tighten when StateDelta is wired
                            beats_fired=_beats_fired,
                            extraction_tier=1,  # TODO: map result.prompt_tier to int tier
                            token_count_in=result.token_count_in or 0,
                            token_count_out=result.token_count_out or 0,
                            agent_duration_ms=result.agent_duration_ms or 0,
                            is_degraded=result.is_degraded,
                            phase_durations_ms=timings.to_dict(),
                            phase_call_counts=timings.phase_call_counts,
                            total_duration_ms=timings.total_ms,
                            footnotes_count=len(result.footnotes or []),
                        )
                        await self._validator.submit(record)
                        submitted = True
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("turn_record.assemble_failed: %s", exc)

                # ADR-024 / story 81-2: feed the per-session TensionTracker one
                # observation per turn so the dual-track pacing signal accumulates
                # across the session and the tension:round_observed watcher event
                # fires every turn (the GM-panel pacing lie-detector). Guarded like
                # the sibling per-turn watcher emits below — a tension/watcher
                # hiccup must never crash the turn (ADR-006 graceful degradation).
                try:
                    _drive_session_tension_tracker(
                        sd,
                        snapshot,
                        encounter_resolved_this_turn=encounter_resolved_this_turn,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("tension_tracker.drive_failed: %s", exc)

                # Per-turn game_state_snapshot for the dashboard State tab
                # (playtest 2026-04-30 #1C). Pre-fix it fired only at connect, so
                # the State tab waited forever. ADR-031 wants a per-turn tick.
                try:
                    _watcher_publish(
                        "game_state_snapshot",
                        {
                            "reason": "turn",
                            "genre_slug": sd.genre_slug,
                            "world_slug": sd.world_slug,
                            "player_name": sd.player_name,
                            "player_id": sd.player_id,
                            "turn_number": snapshot.turn_manager.interaction,
                            # Full snapshot dump so the State panel can render its
                            # rich UI (pre-fix the payload was counts-only).
                            "snapshot": snapshot.model_dump(mode="json"),
                            # Back-compat summary fields. Wave 2B (45-48):
                            # per-actor location (the dashboard is per-player).
                            "current_location": (
                                snapshot.party_location(
                                    perspective=snapshot.player_seats.get(sd.player_id, "")
                                )
                                or snapshot.party_location()
                                or ""
                            ),
                            "discovered_regions": list(snapshot.discovered_regions),
                            "npc_pool_count": len(snapshot.npc_pool),
                            "quest_log_count": len(snapshot.quest_log),
                            "lore_established_count": len(snapshot.lore_established),
                            "character_count": len(snapshot.characters),
                        },
                        component="game",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "game_state_snapshot.publish_failed turn=%d error=%s",
                        snapshot.turn_manager.interaction,
                        exc,
                    )

                return outbound
        finally:
            # Capture timings even when the turn raised — phase data is the
            # diagnostic signal we most need on failure paths.
            import contextlib  # noqa: PLC0415 — local import keeps module import lean

            with contextlib.suppress(Exception):  # finally must never re-raise
                timings.mark_done()

            # Per-turn watcher→OTLP bridge diagnostic + flush: (1) a non-zero
            # ``minted`` proves the bridge fired this turn; (2) force a tracer
            # flush so the BatchSpanProcessor doesn't hide turn-level spans.
            # Suppressed — diagnostics must NEVER fail a turn.
            with contextlib.suppress(Exception):
                minted = synthetic_spans_count() - bridge_minted_at_start
                logger.info(
                    "turn.bridge_diagnostic minted=%d turn=%d player=%s genre=%s world=%s",
                    minted,
                    snapshot.turn_manager.interaction,
                    sd.player_id,
                    sd.genre_slug,
                    sd.world_slug,
                )
            with contextlib.suppress(Exception):
                provider = trace.get_tracer_provider()
                # force_flush is on the SDK TracerProvider but not the proxy
                # provider used in tests; hasattr avoids importing the SDK class.
                flush = getattr(provider, "force_flush", None)
                if callable(flush):
                    flush(timeout_millis=200)

            if not submitted and self._validator is not None:
                try:
                    degraded_record = TurnRecord(
                        turn_id=snapshot.turn_manager.interaction,
                        timestamp=datetime.now(UTC),
                        player_id=sd.player_id,
                        player_input=action,
                        classified_intent=(
                            (
                                getattr(getattr(result, "action_rewrite", None), "intent", "") or ""
                            ).strip()
                            or "unspecified"
                        ),
                        agent_name="narrator",
                        narration="",
                        patches_applied=[],
                        snapshot_before_hash=snapshot_before_hash,
                        snapshot_after=snapshot,
                        delta=None,
                        beats_fired=[],
                        extraction_tier=0,
                        token_count_in=0,
                        token_count_out=0,
                        agent_duration_ms=0,
                        is_degraded=True,
                        phase_durations_ms=timings.to_dict(),
                        phase_call_counts=timings.phase_call_counts,
                        total_duration_ms=timings.total_ms,
                    )
                    await self._validator.submit(degraded_record)
                except Exception:  # noqa: BLE001
                    logger.exception("turn_record.degraded_submit_failed")

    async def _run_opening_turn_narration(
        self,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        """Fire the opening narration turn at the end of chargen.

        Consumes ``sd.opening_seed`` + ``sd.opening_directive`` once (seed →
        first action, directive → narrator Early zone), then zeroes both. When
        no opening hook was resolved, substitutes a generic "I look around…" so
        the narrator still fires.
        """
        # Consume-time MP-joiner suppression (playtest 2026-04-26 coyote_star
        # regression). The connect-time guard misses the both-in-lobby race; by
        # the time we get here the joiner's PC is in snapshot.characters, so
        # >1 character means suppress the cold-open and fall back to the generic
        # continuation action (ADR-067 scene continuation, not a fresh open).
        if sd.opening_seed is not None and len(sd.snapshot.characters) > 1:
            _watcher_publish(
                "mp_joiner_opening_suppressed_at_consume",
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player_id": player_id,
                    "player_name": sd.player_name,
                    "character_count": len(sd.snapshot.characters),
                    "had_seed": True,
                    "had_directive": sd.opening_directive is not None,
                },
                component="opening_hook",
                severity="info",
            )
            logger.info(
                "session.mp_joiner_opening_suppressed_at_consume "
                "genre=%s world=%s player=%s character_count=%d",
                sd.genre_slug,
                sd.world_slug,
                sd.player_name,
                len(sd.snapshot.characters),
            )
            span.add_event(
                "mp_joiner_opening_suppressed_at_consume",
                {
                    "event": "mp_joiner_opening_suppressed_at_consume",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player_id": player_id,
                    "character_count": len(sd.snapshot.characters),
                },
            )
            sd.opening_seed = None
            sd.opening_directive = None

        # Playtest 2026-04-29: on MP joiner-orientation the old generic fallback
        # gave the narrator an unattributed action and it narrated the host PC's
        # POV instead. Naming the joining PC fixes the attribution.
        joiner_orientation = sd.opening_seed is None and len(sd.snapshot.characters) > 1
        if joiner_orientation:
            # Per-arrival entry beat (sq-playtest 2026-05-12): anchor POV on the
            # just-joined PC. The prior >=3 omniscient branch suppressed per-PC
            # POV for the 3rd+ commit (Carl/Donut/Katia bug). Resolve the joiner
            # from the seat-map, falling back to characters[-1] for legacy paths.
            joiner_char_name = sd.snapshot.player_seats.get(sd.player_id or "", "") or (
                sd.snapshot.characters[-1].core.name
                if sd.snapshot.characters
                else (sd.player_name or "the new arrival")
            )
            # Playtest 2026-05-02: joiner-orientation drifted off the
            # established scene. Anchor the joiner to the host's already-
            # established location so the narrator doesn't invent a new place;
            # fall back to "the same scene the others are in" when no seated PC
            # has a known location. Wave 2B (45-48): read the location from any
            # already-seated non-joiner PC (party_location() is None pre-entry).
            host_location = ""
            for _seated_char in sd.snapshot.player_seats.values():
                if _seated_char and _seated_char != joiner_char_name:
                    _here = sd.snapshot.character_locations.get(_seated_char)
                    if _here:
                        host_location = _here.strip()
                        break
            where_clause = (
                f"into the location the prior turn established ({host_location!r})"
                if host_location
                else "into the same scene the other player(s) are already in"
            )
            # For 3+ PCs, name the other seated PCs so the narrator has the full
            # table in view when describing the arrival.
            other_pcs = [
                n for n in sd.snapshot.player_seats.values() if n and n != joiner_char_name
            ]
            other_pcs_clause = (
                f" The other PCs already in the scene: {', '.join(other_pcs)}."
                if len(other_pcs) >= 2
                else ""
            )
            action = (
                f"{joiner_char_name} steps into the scene and orients to "
                f"the surroundings — describe their arrival {where_clause} "
                "from their point of view in a brief grounding paragraph."
                f"{other_pcs_clause} "
                "Do NOT relocate them to a new location. Do NOT generate "
                "dialogue, decisions, or new actions for any other PC "
                "already present."
            )
            source_tier = "mp_joiner_orientation"
            # OTEL: surface the anchor decision (CLAUDE.md OTEL principle) so the
            # GM panel can verify the joiner's prompt carried the host's location.
            # Mirrored as span.add_event so OTLP exporters see it without
            # SIDEQUEST_WATCHER_AS_SPANS=1.
            anchor_kind = "host_location" if host_location else "fallback_same_scene"
            _watcher_publish(
                "mp_joiner_orientation_anchored",
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "joiner_char_name": joiner_char_name,
                    "host_location": host_location or None,
                    "anchor_kind": anchor_kind,
                    "seated_count": len(other_pcs) + 1,
                },
                component="opening_hook",
                severity="info",
            )
            span.add_event(
                "mp_joiner_orientation_anchored",
                {
                    "event": "mp_joiner_orientation_anchored",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "joiner_char_name": joiner_char_name,
                    "host_location": host_location or "",
                    "anchor_kind": anchor_kind,
                    "seated_count": len(other_pcs) + 1,
                },
            )
        else:
            action = sd.opening_seed or "I look around and take in my surroundings."
            source_tier = "world_or_genre_hook" if sd.opening_seed else "fallback"

        # Cold-open delivery (playtest 2026-04-25 [P2]). The opening seed is
        # authored prose for the player to READ, not narrator prompt-context;
        # passing it only as `action` truncated long hooks. Fix: emit the seed as
        # a NARRATION message BEFORE the narrator runs (which still receives it as
        # `action` and continues from there) so the player sees hook + continuation
        # as one beat. Suppressed when the pack has no opening hook.
        cold_open_messages: list[object] = []
        if sd.opening_seed:
            cold_open_messages.append(
                NarrationMessage(
                    payload=NarrationPayload(text=NonBlankString(sd.opening_seed)),
                )
            )
            _watcher_publish(
                "cold_open_emitted",
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "seed_len": len(sd.opening_seed),
                },
                component="opening_hook",
                severity="info",
            )

        lore_context = await self._retrieve_lore_for_turn(sd, action)
        entity_retrieval = await self._retrieve_entities_for_turn(sd, action)
        turn_context = _build_turn_context(
            sd,
            opening_directive=sd.opening_directive,
            lore_context=lore_context,
            entity_retrieval=entity_retrieval,
            room=self._room,
        )

        span.add_event(
            "opening_turn.dispatched",
            {
                "event": "opening_turn.dispatched",
                "has_directive": sd.opening_directive is not None,
                "seed_source": source_tier,
                "action_len": len(action),
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "cold_open_emitted": bool(cold_open_messages),
            },
        )

        narrator_messages = await self._execute_narration_turn(
            sd,
            action,
            turn_context,
            is_opening_turn=True,
        )

        # Story 71-13 / sq-playtest 2026-05-28 #G1: route the cold-open prose
        # through the EventLog HERE so it is event-sourced, projected, and
        # replayable — the same pipeline the narrator's own NARRATION already
        # used inside _execute_narration_turn. The narrator_messages are
        # ALREADY emitted (NARRATION event-sourced internally; RENDER_QUEUED /
        # NARRATION_END / AUDIO_CUE appended as non-durable frames), so the
        # caller must NOT re-emit them. The prior caller-side loop blindly
        # re-emitted EVERY opening message as kind="NARRATION", which persisted
        # a RenderQueuedPayload (`{render_id}`) under NARRATION and bricked
        # reconnect: replay rebuilt it as NarrationPayload, failed validation,
        # and tore down the socket on every reconnect.
        _opening_connected = (
            self._room.connected_player_ids()
            if self._room is not None
            and callable(getattr(self._room, "connected_player_ids", None))
            else []
        )
        _cold_open_author = sd.player_id if len(_opening_connected) > 1 else None
        emitted_cold_open = [
            self._emit_event("NARRATION", m.payload, author_player_id=_cold_open_author)
            for m in cold_open_messages
        ]
        messages = emitted_cold_open + list(narrator_messages)

        # Canned-openings Phase 4 (Task 19): emit opening.played at consumption
        # so the GM panel can verify the opening reached the narrator's first
        # turn. Only fires when a directive was rendered.
        if sd.opening_directive is not None:
            record_opening_played(
                opening_id=getattr(sd, "_resolved_opening_id", None) or "<unknown>",
                turn_id=sd.snapshot.turn_manager.interaction,
            )

        # Consume once — subsequent turns run directive- and seed-free.
        sd.opening_seed = None
        sd.opening_directive = None

        return messages

    # ------------------------------------------------------------------
    # Visual-scene render dispatch
    # ------------------------------------------------------------------

    def _maybe_dispatch_render(
        self,
        sd: _SessionData,
        result: object,
        *,
        encounter_resolved_this_turn: bool = False,
        snapshot_location_before: str | None = None,
        acting_character_name: str | None = None,
    ) -> RenderQueuedMessage | None:
        """Fire a render request if the trigger policy rates this turn eligible
        (Story 45-30).

        Returns a ``RenderQueuedMessage`` to append to outbound frames, or
        ``None`` when nothing dispatched (none_policy, flag off, daemon offline,
        no queue). The daemon round-trip runs on a background task; failures are
        swallowed with OTEL/watcher events so no render error crashes a turn.

        Args:
            encounter_resolved_this_turn: ``True`` when an encounter resolved
                this turn (threaded from the narration_apply seam).
            snapshot_location_before: the acting PC's location pre-apply, which
                the classifier needs to detect SCENE_CHANGE.
        """
        from sidequest.agents.orchestrator import NarrationTurnResult
        from sidequest.server.render_trigger import (
            RenderTriggerReason,
            classify_trigger,
        )

        if not isinstance(result, NarrationTurnResult):
            return None

        visual = result.visual_scene
        had_visual_scene = visual is not None
        subject_present = had_visual_scene and bool(getattr(visual, "subject", "").strip())

        # Policy gate (Story 45-30) — classify from the structured signals on
        # NarrationTurnResult + encounter_resolved_this_turn. The visual_scene
        # block is NOT a signal (pre-story it let banter render but skipped
        # named-NPC introductions).
        location_before = (
            snapshot_location_before
            if snapshot_location_before is not None
            else sd.snapshot.party_location(perspective=perspective_character_name(sd))
        )
        reason = classify_trigger(
            result,
            snapshot_location_before=location_before,
            encounter_resolved_this_turn=encounter_resolved_this_turn,
        )

        turn_number = sd.snapshot.turn_manager.interaction

        # NONE_POLICY: emit render.trigger (eligible=False) AND render.policy_skip
        # so the GM panel gets negative confirmation the policy ran (CLAUDE.md
        # OTEL principle — silence is the bug).
        if reason is RenderTriggerReason.NONE_POLICY:
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "trigger",
                    "reason": reason.value,
                    "eligible": False,
                    "queued": False,
                    "turn_number": turn_number,
                    "player_id": sd.player_id,
                    "had_visual_scene": had_visual_scene,
                    "subject_present": subject_present,
                },
                component="render",
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "policy_skip",
                    "reason": reason.value,
                    "turn_number": turn_number,
                    "player_id": sd.player_id,
                    # "narrator didn't try" vs "emitted a subject, no policy match".
                    "narrator_emitted_subject": subject_present,
                },
                component="render",
            )
            return None

        # Eligible — emit the trigger event before any downstream gate so the GM
        # panel sees the policy decision even when a gate refuses below.
        _watcher_publish(
            "state_transition",
            {
                "field": "render",
                "op": "trigger",
                "reason": reason.value,
                "eligible": True,
                # Optimistic True; a downstream synchronous refusal emits its
                # own watcher event rather than editing this one.
                "queued": True,
                "turn_number": turn_number,
                "player_id": sd.player_id,
                "had_visual_scene": had_visual_scene,
                "subject_present": subject_present,
            },
            component="render",
        )

        # Eligible turns still need a visual_scene subject to compose a prompt.
        # When the narrator emitted none, we cannot dispatch — log loudly.
        if not subject_present:
            logger.warning(
                "render.eligible_no_subject reason=%s turn=%d — "
                "policy fired but narrator emitted no visual_scene subject",
                reason.value,
                turn_number,
            )
            return None

        if not render_enabled():
            logger.info("render.skipped reason=feature_flag_disabled")
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "skipped",
                    "reason": "feature_flag_disabled",
                    "turn_number": sd.snapshot.turn_manager.interaction,
                },
                component="render",
            )
            return None
        client = DaemonClient()
        # Story 45-31: render_unavailable_pending was already stamped before the
        # scrapbook emit (which carries render_status="unavailable"). Here just
        # emit the watcher event, bump counters, and skip the round-trip.
        if sd.render_unavailable_pending:
            from sidequest.daemon_client.state_mirror import get_mirror as _get_mirror

            _mirror = _get_mirror()
            sd.render_unresponsive_window_count += 1
            logger.warning(
                "render.unavailable reason=heartbeat_lost last_ts=%s turn=%d",
                _mirror.last_heartbeat_ts(),
                sd.snapshot.turn_manager.interaction,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "unavailable",
                    "reason": "heartbeat_lost",
                    "last_heartbeat_ts": _mirror.last_heartbeat_ts(),
                    "turn_number": sd.snapshot.turn_manager.interaction,
                    "player_id": sd.player_id,
                },
                component="render",
                severity="warning",
            )
            return None
        if not client.is_available():
            logger.warning(
                "render.skipped reason=daemon_unavailable socket=%s",
                client.socket_path,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "skipped",
                    "reason": "daemon_unavailable",
                    "socket": str(client.socket_path),
                    "turn_number": sd.snapshot.turn_manager.interaction,
                },
                component="render",
                severity="warning",
            )
            return None
        if self._out_queue is None:
            # No room context (test config) — nowhere for the IMAGE to land.
            logger.warning("render.skipped reason=no_outbound_queue")
            return None

        # ADR-050 image pacing throttle. Consult BEFORE allocating a render_id
        # or touching the daemon — suppressed renders leave only the OTEL event.
        throttle_decision = sd.image_pacing_throttle.should_render()
        provisional_render_id = uuid.uuid4().hex[:12]
        if not throttle_decision.allowed:
            logger.info(
                "render.throttled render_id=%s reason=%s remaining=%ds",
                provisional_render_id,
                throttle_decision.reason,
                throttle_decision.cooldown_remaining_seconds,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "throttle_decision",
                    "decision": "suppress",
                    "reason": throttle_decision.reason,
                    "render_id": provisional_render_id,
                    "cooldown_remaining_seconds": (throttle_decision.cooldown_remaining_seconds),
                    "cooldown_seconds": sd.image_pacing_throttle.cooldown_seconds,
                    "turn_number": sd.snapshot.turn_manager.interaction,
                },
                component="render",
            )
            return None
        # Allowed — emit the allow decision so the GM panel sees both branches
        # (CLAUDE.md OTEL lie-detector requirement).
        _watcher_publish(
            "state_transition",
            {
                "field": "render",
                "op": "throttle_decision",
                "decision": "allow",
                "reason": throttle_decision.reason,
                "render_id": provisional_render_id,
                "cooldown_seconds": sd.image_pacing_throttle.cooldown_seconds,
                "turn_number": sd.snapshot.turn_manager.interaction,
            },
            component="render",
        )

        render_id = provisional_render_id
        tier = (visual.tier or "scene_illustration").strip() or "scene_illustration"

        # The location is free-form narrator prose, not a `where:<slug>`
        # PlaceCatalog ref. The daemon's _resolve_location accepts "" but rejects
        # anything non-ref with ValueError → COMPOSE_FAILED. Sanitize centrally:
        # only true `where:<slug>` refs survive; free-form prose drops to "" with
        # a loud watcher event (no silent fallback). Wave 2B (45-48): source is
        # the acting PC's location, party consensus as fallback.
        raw_location = (
            sd.snapshot.party_location(perspective=acting_character_name)
            if acting_character_name
            else sd.snapshot.party_location()
        ) or ""
        raw_location = raw_location.strip()
        sanitized_location = raw_location if raw_location.startswith("where:") else ""
        if raw_location and not sanitized_location:
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "location_dropped",
                    "reason": "free_form_prose_no_catalog_ref",
                    "raw_location": raw_location[:120],
                    "tier": tier,
                    "render_id": render_id,
                    "turn_number": sd.snapshot.turn_manager.interaction,
                },
                component="render",
                severity="info",
            )

        # R2 migration Task 20: propagate the session id so the upload key
        # ``artifacts/<world>/<session>/<kind>/<sha>.<ext>`` carries a real
        # session segment (the daemon falls back to "unknown" when missing,
        # defeating per-session bucketing). Per "No Silent Fallbacks", refuse to
        # dispatch with a missing session id rather than use a placeholder.
        if self._room is not None:
            session_id = self._room.slug
        elif sd.game_slug is not None:
            session_id = sd.game_slug
        else:
            raise RuntimeError(
                "render dispatch fired without a bound session id "
                "(no room and no sd.game_slug) — slug-connect path "
                "should have populated this. Refusing to dispatch with "
                "a fallback that would corrupt R2 artifact keying."
            )
        params: dict[str, object] = {
            "tier": tier,
            "subject": visual.subject,
            "mood": visual.mood or "",
            "tags": list(visual.tags or []),
            "location": sanitized_location,
            "narration": result.narration,
            "genre": sd.genre_slug,
            # Catalog-injected compose (slice 1): the daemon scopes its catalogs
            # by (genre, world). Without ``world`` the compose conditional is
            # dead and every render falls through to the prose-subject path with
            # no world style (Bug #2a, playtest 2026-04-26 — generic grimvault
            # renders). Sending it engages the world-scoped visual_style suffix.
            "world": sd.world_slug,
            # R2 migration Task 20 — see preamble above.
            "session_id": session_id,
        }
        # Portrait initials overlay (story 37-30 AC-4): the portrait composer
        # needs the display name for the initials card. Other tiers ignore it.
        if tier == "portrait":
            params["subject_name"] = sd.player_name
            # Catalog-injected compose, slice 2: a `pc:<slug>` ref routes the
            # portrait through the catalog instead of the prose-subject path; ship
            # a descriptor blob for the daemon's CharacterCatalog.add_pc.
            pc_slug = _slugify_player_name(sd.player_name)
            params["characters"] = [f"pc:{pc_slug}"]
            descriptor = _build_pc_descriptor(sd, pc_slug)
            if descriptor is not None:
                params["pc_descriptor"] = descriptor
        elif tier == "scene_illustration":
            pc_slug = _slugify_player_name(sd.player_name)
            # `characters` is the field the daemon reads (it once read
            # `participants` here, which the daemon ignored, so the PC ref never
            # reached the composer). The daemon routes to recipes by tier.
            params["characters"] = [f"pc:{pc_slug}"]
            descriptor = _build_pc_descriptor(sd, pc_slug)
            if descriptor is not None:
                params["pc_descriptor"] = descriptor

        # Story 37-30 — record (room_slug, player_id) at dispatch so the
        # completion handler routes the IMAGE via the live RoomRegistry queue,
        # not a closure-captured one that may be stale after a reconnect.
        room_slug = self._room.slug if self._room is not None else None
        player_id = sd.player_id
        # Playtest 2026-05-02: capture the dispatch-time turn_id so completion
        # can backfill the scrapbook row's image_url (the broadcast is ephemeral;
        # replay-on-reload misses every IMAGE without this).
        dispatch_turn_id = int(sd.snapshot.turn_manager.interaction)

        logger.info(
            "render.dispatched render_id=%s tier=%s subject=%r",
            render_id,
            tier,
            visual.subject[:80],
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "render",
                "op": "dispatched",
                "render_id": render_id,
                "tier": tier,
                "subject": visual.subject[:120],
                "turn_number": sd.snapshot.turn_manager.interaction,
                "player_id": player_id,
                "room_slug": room_slug or "",
                # Bug #2a lie-detector: surface the genre/world the daemon sees.
                # Empty ``world`` means the compose gate short-circuits to a
                # styleless prompt.
                "genre": sd.genre_slug,
                "world": sd.world_slug,
            },
            component="render",
        )

        # Story 45-31: backpressure check (orthogonal to the ADR-050 throttle's
        # time gate) — warns on concurrent in-flight depth before a swamped
        # daemon piles on. Threshold 3; warn-mode lets the request through.
        sd.render_enqueue_count += 1
        in_flight_after = sd.render_in_flight + 1
        backpressure_threshold = 3
        if in_flight_after > backpressure_threshold:
            sd.render_backpressure_warn_count += 1
            logger.warning(
                "render.enqueue.backpressure render_id=%s queue_depth=%d threshold=%d",
                render_id,
                in_flight_after,
                backpressure_threshold,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "enqueue.backpressure",
                    "decision": "warn",
                    "queue_depth": in_flight_after,
                    "threshold": backpressure_threshold,
                    "render_id": render_id,
                    "turn_number": sd.snapshot.turn_manager.interaction,
                    "player_id": sd.player_id,
                },
                component="render",
                severity="warning",
            )
        # _run_render decrements this in its finally block.
        sd.render_in_flight = in_flight_after

        # legacy_queue is the pre-room-context fallback; when _room is set the
        # completion handler looks the queue up via the registry instead.
        legacy_queue = self._out_queue if room_slug is None else None
        asyncio.create_task(
            self._run_render(
                client,
                params,
                render_id,
                room_slug,
                player_id,
                legacy_queue,
                dispatch_turn_id,
                sd,
            )
        )
        # ADR-050 — record the dispatch after the task is created so the cooldown
        # only ticks on actually-dispatched renders (force_render skips this).
        sd.image_pacing_throttle.record_render()

        return RenderQueuedMessage(
            type=MessageType.RENDER_QUEUED,  # type: ignore[arg-type]
            payload=RenderQueuedPayload(render_id=render_id),
            player_id=player_id,
        )

    # ------------------------------------------------------------------
    # Lore embedding — RAG retrieval (pre-turn) + worker dispatch (post-turn)
    # ------------------------------------------------------------------

    async def _retrieve_lore_for_turn(self, sd: _SessionData, action: str) -> str | None:
        """Pre-turn lore RAG retrieval. Delegates to ``lore_embed.retrieve_for_turn``."""
        from sidequest.server.dispatch import lore_embed

        return await lore_embed.retrieve_for_turn(self, sd, action)

    async def _retrieve_entities_for_turn(self, sd: _SessionData, action: str) -> RetrievedEntities:
        """Pre-turn universal entity retrieval (ADR-118 §D4/§D5, Stories 75-5/75-7).

        Delegates to ``universal_retrieval.retrieve_for_turn``: the typed sibling
        of :meth:`_retrieve_lore_for_turn` assembles the floor (scene-present
        NPCs) and the semantic fill under a per-turn token budget, sanitizing
        retrieved content at the choke-point, emitting the ``retrieval.universal``
        span, AND (75-7) publishing the decision as a watcher event so it reaches
        the GM panel. Never raises.
        """
        from sidequest.server.dispatch import universal_retrieval

        return await universal_retrieval.retrieve_for_turn(self, sd, action)

    def _dispatch_embed_worker(self, sd: _SessionData) -> None:
        """Post-turn embed worker dispatch. Delegates to ``lore_embed.dispatch_worker``."""
        from sidequest.server.dispatch import lore_embed

        lore_embed.dispatch_worker(self, sd)

    def _accrete_lore_for_turn(self, sd: _SessionData) -> None:
        """Post-turn lore accretion. Delegates to ``lore_accretion.accrete_for_turn``."""
        from sidequest.server.dispatch import lore_accretion

        lore_accretion.accrete_for_turn(self, sd)

    def _sync_entity_cards_for_turn(self, sd: _SessionData) -> None:
        """Post-turn entity-card sync. Delegates to ``entity_sync.sync_for_turn`` (75-6)."""
        from sidequest.server.dispatch import entity_sync

        entity_sync.sync_for_turn(self, sd)

    async def _run_embed_worker(
        self, sd: _SessionData, pending_count: int, turn_number: int
    ) -> None:
        """Background embed worker. Delegates to ``lore_embed.run_worker``."""
        from sidequest.server.dispatch import lore_embed

        await lore_embed.run_worker(self, sd, pending_count, turn_number)

    async def _run_render(
        self,
        client: DaemonClient,
        params: dict[str, object],
        render_id: str,
        room_slug: str | None,
        player_id: str,
        legacy_queue: asyncio.Queue[object] | None,
        dispatch_turn_id: int,
        sd: _SessionData | None = None,
    ) -> None:
        """Background render coroutine — waits for the daemon reply, enqueues an
        IMAGE or logs a failure. Never raises (exceptions become OTEL events).

        Routing (story 37-30): with ``room_slug`` set, the IMAGE goes to the
        current outbound queue via the RoomRegistry so a mid-render reconnect
        still gets it; ``legacy_queue`` is the pre-room-context fallback. ``sd``
        optional — when present, the in-flight counter (45-31) decrements in finally.
        """
        try:
            await self._run_render_inner(
                client,
                params,
                render_id,
                room_slug,
                player_id,
                legacy_queue,
                dispatch_turn_id,
                sd,
            )
        finally:
            if sd is not None:
                # Unconditional — completed, failed, or raised all release the slot.
                sd.render_in_flight = max(0, sd.render_in_flight - 1)

    async def _run_render_inner(
        self,
        client: DaemonClient,
        params: dict[str, object],
        render_id: str,
        room_slug: str | None,
        player_id: str,
        legacy_queue: asyncio.Queue[object] | None,
        dispatch_turn_id: int,
        sd: _SessionData | None = None,
    ) -> None:
        """Inner body of ``_run_render`` — the daemon round-trip + IMAGE fan-out.
        Split out so the 45-31 counter decrement lives in one finally block.
        """
        try:
            reply = await client.render(params)
        except DaemonUnavailableError as exc:
            logger.warning("render.reply_unavailable render_id=%s error=%s", render_id, exc)
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "failed",
                    "render_id": render_id,
                    "reason": "daemon_unavailable",
                    "error": str(exc),
                },
                component="render",
                severity="warning",
            )
            return
        except DaemonRequestError as exc:
            logger.warning(
                "render.reply_error render_id=%s code=%s error=%s",
                render_id,
                exc.code,
                exc.message,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "failed",
                    "render_id": render_id,
                    "reason": "daemon_error",
                    "code": exc.code,
                    "error": exc.message,
                },
                component="render",
                severity="error",
            )
            return
        except Exception as exc:  # noqa: BLE001 — background task must never crash the loop
            logger.exception("render.reply_exception render_id=%s", render_id)
            _watcher_publish(
                "state_transition",
                {
                    "field": "render",
                    "op": "failed",
                    "render_id": render_id,
                    "reason": "exception",
                    "error": type(exc).__name__,
                },
                component="render",
                severity="error",
            )
            return

        image_url = str(reply.get("image_url") or "")
        # R2 migration (Task 11): an ``r2_key`` in the reply means the artifact
        # uploaded to R2 — resolve via the asset_urls seam (CDN URL). Absent →
        # legacy local-tmpdir flow, which needs the self-healing render mount:
        # ensure_render_mount appends a restarted daemon's new tmp dir to the
        # live StaticFiles mount so /renders/* keeps serving (falls back to the
        # env-based rewriter for single-root paths + tests).
        from sidequest.server.render_mounts import (
            ensure_render_mount,
            get_active_app,
            resolve_artifact_url,
        )

        r2_key = reply.get("r2_key")
        if r2_key:
            served_url = resolve_artifact_url(str(r2_key)) or ""
            # Story 65-2: link this runtime artifact to the save that produced
            # it, so the UI can rehydrate it on resume without re-rendering.
            # The content sha256 already lives inside r2_key, so nothing else
            # is needed from the reply. asset_type is the <kind> segment of
            # artifacts/<world>/<session>/<kind>/<sha>.<ext>.
            if sd is not None:
                # The SaveRepository Protocol guarantees append_asset_ledger; no
                # hasattr guard (that would be a silent-skip fallback). A missing
                # method should raise loudly via the except below.
                key_parts = str(r2_key).split("/")
                asset_type = key_parts[3] if len(key_parts) >= 5 else str(params.get("tier") or "")
                entity_ref = str(params.get("subject_name") or params.get("subject") or "unknown")
                try:
                    sd.repository.append_asset_ledger(
                        r2_key=str(r2_key),
                        asset_type=asset_type,
                        entity_ref=entity_ref,
                        created_turn=dispatch_turn_id,
                    )
                except Exception as exc:  # noqa: BLE001 — ledger failure must not lose the image
                    logger.error(
                        "asset_ledger.write_failed render_id=%s r2_key=%s error=%s",
                        render_id,
                        r2_key,
                        exc,
                    )
                    _watcher_publish(
                        "state_transition",
                        {
                            "field": "asset_ledger",
                            "op": "write_failed",
                            "r2_key": str(r2_key),
                            "render_id": render_id,
                            "error": type(exc).__name__,
                        },
                        component="render",
                        severity="error",
                    )
                else:
                    _watcher_publish(
                        "state_transition",
                        {
                            "field": "asset_ledger",
                            "op": "write",
                            "r2_key": str(r2_key),
                            "asset_type": asset_type,
                            "entity_ref": entity_ref,
                            "session_id": sd.repository.session_id,
                            "turn": dispatch_turn_id,
                        },
                        component="render",
                        severity="info",
                    )
        else:
            active_app = get_active_app()
            healed: str | None = (
                ensure_render_mount(active_app, image_url)
                if active_app is not None and image_url
                else None
            )
            served_url = healed if healed is not None else _render_url_from_path(image_url)
        width = int(reply.get("width") or 0) or None
        height = int(reply.get("height") or 0) or None
        elapsed = int(reply.get("elapsed_ms") or 0)

        msg = ImageMessage(
            type=MessageType.IMAGE,  # type: ignore[arg-type]
            payload=ImagePayload(
                url=served_url,
                render_id=render_id,
                tier=str(params.get("tier") or ""),
                width=width,
                height=height,
            ),
            player_id=player_id,
        )

        # Story 37-30 — resolve the live room at completion time via the
        # RoomRegistry so mid-render reconnects land on live sockets.
        # Bug #2b (playtest 2026-04-26): IMAGE used to land on the actor's queue
        # alone; shared-world scene imagery should reach every player, so
        # broadcast to all queues. Legacy single-queue path remains for tests.
        recipients_count = 0
        broadcast_used = False
        if room_slug is not None:
            registry = self._room_registry
            room = registry.get(room_slug) if registry is not None else None
            if room is None:
                # No live room — surface as session_not_found, not a silent drop.
                logger.warning(
                    "render.session_not_found render_id=%s room=%s player=%s reason=room_missing",
                    render_id,
                    room_slug,
                    player_id,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "render",
                        "op": "session_not_found",
                        "render_id": render_id,
                        "room_slug": room_slug,
                        "player_id": player_id,
                        "tier": str(params.get("tier") or ""),
                        "url": served_url,
                        "reason": "room_missing",
                    },
                    component="render",
                    severity="warning",
                )
                return
            # Broadcast to every socket (the originator included, mirroring the
            # _emit_event fan-out). Pingpong 2026-04-30: recipients_count once came
            # from connected_player_ids() while the broadcast iterates
            # _outbound_queues; when those diverge the log over-reported. Now use
            # the broadcast return value (actual (socket_id, player_id) pairs) so
            # the GM panel sees ground truth.
            try:
                delivered_recipients = room.broadcast(msg, exclude_socket_id=None)
            except Exception as exc:  # noqa: BLE001 — broadcast failure must surface
                logger.warning(
                    "render.broadcast_failed render_id=%s error=%s",
                    render_id,
                    exc,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "render",
                        "op": "broadcast_failed",
                        "render_id": render_id,
                        "room_slug": room_slug,
                        "player_id": player_id,
                        "url": served_url,
                        "error": type(exc).__name__,
                    },
                    component="render",
                    severity="error",
                )
                return
            broadcast_used = True
            # Lie-detector: ground-truth count from the broadcast plus the
            # connect-map count, so the GM panel surfaces any divergence.
            recipients_count = len(delivered_recipients)
            connected_count = len(room.connected_player_ids())
            recipient_socket_ids = [sid for sid, _pid in delivered_recipients]
            recipient_player_ids = [pid for _sid, pid in delivered_recipients if pid is not None]
            try:
                _watcher_publish(
                    "scrapbook_image_broadcast",
                    {
                        "render_id": render_id,
                        "slug": room_slug,
                        "originating_player_id": player_id,
                        "url": served_url,
                        "tier": str(params.get("tier") or ""),
                        "recipients_count": recipients_count,
                        "connected_count": connected_count,
                        "queue_connect_divergence": connected_count != recipients_count,
                        "recipient_socket_ids": recipient_socket_ids,
                        "recipient_player_ids": recipient_player_ids,
                    },
                    component="render",
                    severity="warning" if connected_count != recipients_count else "info",
                )
            except Exception as exc:  # noqa: BLE001 — telemetry must never crash a broadcast
                logger.warning(
                    "scrapbook_image_broadcast.watcher_publish_failed render_id=%s error=%s",
                    render_id,
                    exc,
                )
            if recipients_count == 0:
                logger.warning(
                    "render.broadcast_no_recipients render_id=%s room=%s",
                    render_id,
                    room_slug,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "render",
                        "op": "session_not_found",
                        "render_id": render_id,
                        "room_slug": room_slug,
                        "player_id": player_id,
                        "tier": str(params.get("tier") or ""),
                        "url": served_url,
                        "reason": "no_connected_players",
                    },
                    component="render",
                    severity="warning",
                )
                return
        else:
            # Legacy / test path: no room context — use the single queue
            # captured at dispatch.
            target_queue = legacy_queue
            if target_queue is None:
                logger.warning(
                    "render.session_not_found render_id=%s reason=no_queue",
                    render_id,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "render",
                        "op": "session_not_found",
                        "render_id": render_id,
                        "player_id": player_id,
                        "tier": str(params.get("tier") or ""),
                        "url": served_url,
                        "reason": "no_outbound_queue",
                    },
                    component="render",
                    severity="warning",
                )
                return
            try:
                target_queue.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning("render.outbound_queue_full render_id=%s", render_id)
                return
            recipients_count = 1

        # Playtest 2026-05-02: persist the URL into the scrapbook_entries row so
        # replay can JOIN it back on reconnect (the IMAGE broadcast is ephemeral;
        # without this every reload leaves placeholder cards).
        from sidequest.server.emitters import update_scrapbook_image_url

        scrapbook_updated = update_scrapbook_image_url(
            self,
            dispatch_turn_id,
            served_url,
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "scrapbook",
                "op": "image_url_backfilled",
                "render_id": render_id,
                "turn_id": dispatch_turn_id,
                "url": served_url,
                "row_updated": scrapbook_updated,
            },
            component="scrapbook",
        )

        logger.info(
            "render.completed render_id=%s url=%s elapsed_ms=%d recipients=%d broadcast=%s",
            render_id,
            served_url,
            elapsed,
            recipients_count,
            broadcast_used,
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "render",
                "op": "completed",
                "render_id": render_id,
                "url": served_url,
                "elapsed_ms": elapsed,
                "player_id": player_id,
                "room_slug": room_slug or "",
                # Bug #2b lie-detector: surface broadcast vs. legacy single-queue
                # path + recipient count ("image only reached one player" was an
                # invisible regression, playtest 2026-04-26).
                "broadcast": broadcast_used,
                "recipients": recipients_count,
            },
            component="render",
        )
        # Story 45-31: stamp the diagnostic counters with the latest successful
        # render so the post-session snapshot can quote the last image shown.
        if sd is not None:
            from datetime import UTC
            from datetime import datetime as _dt

            sd.last_successful_render_id = render_id
            sd.last_successful_render_ts_iso = _dt.now(UTC).isoformat()
