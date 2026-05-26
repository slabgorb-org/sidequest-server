"""ClientErrorHandler — handles CLIENT_ERROR messages (Story 67-1).

A client whose render subtree crashed (a GameBoard ErrorBoundary catch)
sends a CLIENT_ERROR over its still-open WebSocket. Because a React render
crash does not close the socket, the server would otherwise keep awaiting
that player at the submit-and-wait barrier forever, orphaning the whole
table's in-flight turn.

This handler drops the crashed player from the CURRENT interaction's awaited
count and re-evaluates the barrier. If the players who already submitted now
satisfy the reduced denominator, the turn dispatches through the shared
``dispatch_fired_barrier`` path. The release happens ONLY on an explicit
crash signal — never on a player who is merely present and quiet (CLAUDE.md:
never rush a slow typist).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from sidequest.game.turn import TurnPhase
from sidequest.handlers.player_action import dispatch_fired_barrier
from sidequest.server.session_helpers import _build_turn_context
from sidequest.telemetry.phase_timing import PhaseTimings
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.protocol import GameMessage
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

logger = logging.getLogger(__name__)


class ClientErrorHandler:
    """Release the turn barrier when a client signals a render crash."""

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        if session._session_data is None:
            return []
        room = session._room
        # Solo / legacy (no bound room) has no shared barrier to orphan — a
        # crash signal is a clean no-op.
        if room is None:
            return []

        sd = session._session_data
        snapshot = sd.snapshot
        tm = snapshot.turn_manager
        payload = msg.payload  # type: ignore[attr-defined]
        reason = getattr(payload, "reason", "render_crash")
        component = getattr(payload, "component", "")
        crashed_id = (msg.player_id or sd.player_id or "")  # type: ignore[attr-defined]

        # No turn in flight (nobody has submitted) → nothing to orphan.
        if not room.has_pending_actions():
            logger.info(
                "session.client_error_noop_no_pending player=%s reason=%s slug=%s",
                crashed_id,
                reason,
                room.slug,
            )
            return []
        if tm.get_phase() != TurnPhase.InputCollection:
            return []

        # A crash from a player who ALREADY submitted changes nothing about the
        # awaited set — releasing them would wrongly fire the barrier on peers
        # who still owe their actions. Leave the barrier untouched.
        submitted: set[str] = object.__getattribute__(tm, "_submitted")
        if crashed_id in submitted:
            logger.info(
                "session.client_error_noop_already_submitted player=%s slug=%s",
                crashed_id,
                room.slug,
            )
            return []

        # Drop the crashed player from this interaction's barrier denominator.
        room.mark_crash_released(crashed_id)
        effective = room.playing_player_count() - room.crash_released_count()
        _watcher_publish(
            "mp.player_crash_released",
            {
                "slug": room.slug,
                "player_id": crashed_id,
                "reason": reason,
                "component": component,
                "effective_player_count": effective,
                "submitted_count": len(submitted),
            },
            component="multiplayer",
        )
        logger.info(
            "session.client_error_crash_released player=%s reason=%s effective=%d submitted=%d slug=%s",
            crashed_id,
            reason,
            effective,
            len(submitted),
            room.slug,
        )

        # Re-evaluate the barrier against the reduced denominator. If other
        # awaited players remain, the barrier still waits (return []).
        if not tm.recheck_barrier(effective):
            return []

        # The crash released the last awaited slot — dispatch the buffered
        # submissions. lore_context is None: the dispatched action is the
        # combined pending buffer (assembled at drain), not a single action we
        # could retrieve lore for here.
        turn_context = _build_turn_context(sd, lore_context=None, room=room)
        timings = PhaseTimings(action_received_monotonic=time.monotonic())
        turn_context.phase_timings = timings
        return await dispatch_fired_barrier(
            session,
            sd=sd,
            snapshot=snapshot,
            turn_context=turn_context,
            playing_count=effective,
            timings=timings,
        )


HANDLER = ClientErrorHandler()
