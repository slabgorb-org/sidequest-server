"""WebSocket connection handler for sidequest-server.

Handles the /ws endpoint: accept connections, read frames, dispatch to
session_handler, write outbound messages.

Port of the WebSocket layer in sidequest-server/src/lib.rs
(handle_ws_connection, the reader/writer split).
Phase 1 only — no dice dispatch, no shared session sync, no multiplayer.

MP-02 Task 4: per-socket write queue + PLAYER_PRESENCE broadcast.
Each connection has a dedicated writer task that drains an asyncio.Queue.
The reader loop puts outbound messages into the queue instead of sending
directly, so room.broadcast() can reach other sockets' queues safely.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.websockets import WebSocketState

from sidequest.protocol import GameMessage
from sidequest.protocol.messages import (
    ErrorMessage,
    ErrorPayload,
    GamePausedMessage,
    GamePausedPayload,
    PlayerPresenceMessage,
    PlayerPresencePayload,
)
from sidequest.protocol.types import NonBlankString  # noqa: F401

if TYPE_CHECKING:
    from sidequest.server.session_handler import WebSocketSessionHandler

logger = logging.getLogger(__name__)


async def ws_endpoint(websocket: WebSocket, handler: WebSocketSessionHandler) -> None:
    """WebSocket connection lifecycle — accept, loop, cleanup.

    On PLAYER_ACTION: dispatch through session_handler → emit NARRATION.
    On SESSION_EVENT{connect}: bind genre/world, load or create session.
    On malformed JSON: send ERROR and close (no silent fallback).
    On disconnect: detach outbound queue, disconnect from room, broadcast
      PLAYER_PRESENCE{disconnected} to remaining players, then persist and clean up.
    """
    await websocket.accept()
    socket_id = uuid.uuid4().hex
    registry = websocket.app.state.room_registry
    out_queue: asyncio.Queue[Any] = asyncio.Queue()
    handler.attach_room_context(registry=registry, socket_id=socket_id, out_queue=out_queue)
    logger.info("ws.connection_accepted remote=%s socket=%s", websocket.client, socket_id)

    async def _writer() -> None:
        """Drain the per-socket outbound queue and send each message."""
        while True:
            msg = await out_queue.get()
            await _send_message(websocket, msg)

    writer_task = asyncio.create_task(_writer())

    async def _surface_unexpected(exc: BaseException) -> None:
        # Safety net for unhandled exceptions in handler.handle_message
        # (e.g. a programmer bug, a subsystem raising before the per-handler
        # try/except wraps it). Surface a typed error frame BEFORE the
        # finally-block close so the UI sees a reason instead of silently
        # reconnecting into the same crash. Per playtest 2026-04-25 bug
        # ticket: "WebSocket exception path leaves UI stuck on Reconnecting…
        # with no surfaced reason."
        logger.exception("ws.unexpected_error error=%s", exc)
        await _send_error(
            websocket,
            f"Server error while processing message: {exc}",
            reconnect_required=False,
            code="server_error",
        )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = GameMessage.model_validate_json(raw)  # type: ignore[arg-type]
            except (ValidationError, ValueError) as exc:
                logger.warning("ws.malformed_json error=%s raw_preview=%r", exc, raw[:200])
                await _send_error(
                    websocket,
                    f"Malformed message: {exc}",
                    reconnect_required=False,
                )
                await websocket.close(code=1003)
                return

            logger.debug("ws.message_received type=%s", msg.type)
            outbound: list[Any] = await handler.handle_message(msg)
            for outbound_msg in outbound:
                out_queue.put_nowait(outbound_msg)

    except WebSocketDisconnect as exc:
        logger.info("ws.disconnected code=%s", exc.code)
    except RuntimeError as exc:
        # Starlette's receive_text() raises a bare RuntimeError from its
        # top-of-function not-connected guard ("WebSocket is not
        # connected. Need to call 'accept' first.") when the peer dropped
        # ungracefully and a concurrent writer send already advanced
        # application_state past CONNECTED. That is an *expected* client
        # teardown, not a server fault — log INFO and fall through to the
        # same cleanup as WebSocketDisconnect (playtest 2026-05-17
        # [BS-BUG-LOW]: this fired ~1712× as ws.unexpected_error,
        # burying genuine errors). Discriminate on the same socket state
        # Starlette itself checks — never on the message string — so a
        # RuntimeError raised while still CONNECTED (a genuine handler
        # bug) still surfaces loudly (No Silent Fallbacks).
        if (
            websocket.application_state != WebSocketState.CONNECTED
            or websocket.client_state == WebSocketState.DISCONNECTED
        ):
            logger.info("ws.disconnected_ungraceful detail=%s", exc)
        else:
            await _surface_unexpected(exc)
    except Exception as exc:
        await _surface_unexpected(exc)
    finally:
        writer_task.cancel()
        room = handler.current_room()
        if room is not None:
            room.detach_outbound(socket_id)
            left_player = room.disconnect(socket_id=socket_id)
            if left_player is not None:
                room.broadcast(
                    _presence_msg(left_player, "disconnected"),
                    exclude_socket_id=socket_id,
                )
                # After the disconnect presence broadcast, check whether the room
                # is now paused (MP-02 Task 6). If so, broadcast GAME_PAUSED to
                # all remaining connected players so they know narration is
                # suspended until the absent player(s) return.
                if room.is_paused():
                    absent = room.absent_seated_player_ids()
                    room.broadcast(
                        GamePausedMessage(payload=GamePausedPayload(waiting_for=absent)),
                        exclude_socket_id=None,
                    )
                # Story 61-followup-C: when the last player disconnects, call
                # close_store() to reset the narrator's cost baselines for the
                # next session. RoomRegistry never evicts, so the orchestrator
                # (and its SDK client) persist across multiple sessions on the
                # same slug; without resetting the baselines, the rolling
                # average can self-train onto a sustained runaway (61-4 +
                # 61-followup-A). check room is now fully empty before tearing
                # down; intermediate disconnects in multiplayer must not
                # trigger close_store (room still has other connected players).
                if not room.connected_player_ids():
                    room.close_store()
                    logger.info("ws.room_teardown_close_store slug=%s", room.slug)
        await handler.cleanup()
        logger.info("ws.session_cleanup_complete")


def _presence_msg(player_id: str, state: str) -> PlayerPresenceMessage:
    """Build a PLAYER_PRESENCE message for connect/disconnect events."""
    return PlayerPresenceMessage(
        payload=PlayerPresencePayload(player_id=player_id, state=state),  # type: ignore[arg-type]
    )


async def _send_message(websocket: WebSocket, msg: Any) -> None:
    """Serialize and send a protocol message object over the WebSocket.

    All outbound messages are pydantic BaseModel instances with model_dump_json().

    Tab-refresh / disconnect short-circuit (playtest 2026-05-02 [BUG-LOW]):
    when the application_state has already advanced past CONNECTED — i.e.
    the close frame has been sent or the peer disconnected — Starlette's
    ``send`` raises ``RuntimeError("Cannot call 'send' once a close
    message has been sent.")``. That's a normal lifecycle event during
    tab focus-switching while broadcasts are mid-fan-out, not a fault.
    Skip the send entirely with a DEBUG breadcrumb so the GM panel keeps
    visibility (CLAUDE.md OTEL principle) without spamming WARNING
    "ws.send_failed" lines that look like real failures.
    """
    if websocket.application_state != WebSocketState.CONNECTED:
        logger.debug(
            "ws.send_skipped_closing type=%s state=%s",
            getattr(msg, "type", "?"),
            websocket.application_state.name,
        )
        return
    try:
        json_str = msg.model_dump_json()
        await websocket.send_text(json_str)
    except Exception as exc:
        logger.warning("ws.send_failed type=%s error=%s", getattr(msg, "type", "?"), exc)


async def _send_error(
    websocket: WebSocket,
    message: str,
    reconnect_required: bool = False,
    *,
    code: str | None = None,
) -> None:
    """Send an ERROR message, ignoring send failures (connection may be closing)."""
    try:
        err = ErrorMessage(
            type="ERROR",  # type: ignore[arg-type]
            payload=ErrorPayload(
                message=NonBlankString(message),
                reconnect_required=reconnect_required,
                code=code,
            ),
            player_id="",
        )
        await websocket.send_text(err.model_dump_json())
    except Exception:
        pass
