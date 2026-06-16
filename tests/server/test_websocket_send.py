"""Outbound-send short-circuit on closing/closed sockets.

Playtest 2026-05-02 [BUG-LOW]: tab-refresh during a broadcast fan-out
produced WARNING-level "ws.send_failed" log spam because the server
kept calling ``websocket.send_text`` after the close frame had been
sent. The fix in ``sidequest.server.websocket._send_message`` checks
``application_state`` before sending — these tests are the wiring
guard so the check stays in front of the try/except.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from sidequest.server.websocket import _send_message


def _msg(type_str: str = "GAME_PAUSED") -> SimpleNamespace:
    return SimpleNamespace(
        type=type_str,
        model_dump_json=lambda: '{"type":"' + type_str + '"}',
    )


@pytest.mark.asyncio
async def test_send_skipped_when_application_state_disconnected() -> None:
    """A websocket whose application_state has already advanced past
    CONNECTED must NOT receive ``send_text`` — Starlette raises in
    that case and the WARNING is misleading log noise.
    """
    ws = SimpleNamespace(
        application_state=WebSocketState.DISCONNECTED,
        send_text=AsyncMock(),
    )

    await _send_message(ws, _msg("PLAYER_PRESENCE"))

    ws.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_passes_through_when_application_state_connected() -> None:
    """Sanity / wiring guard: the short-circuit must not block live
    sockets. A CONNECTED websocket receives the serialized JSON.
    """
    ws = SimpleNamespace(
        application_state=WebSocketState.CONNECTED,
        send_text=AsyncMock(),
    )

    await _send_message(ws, _msg("GAME_PAUSED"))

    ws.send_text.assert_awaited_once_with('{"type":"GAME_PAUSED"}')


@pytest.mark.asyncio
async def test_send_skipped_when_application_state_connecting() -> None:
    """Pre-handshake CONNECTING state — the socket is not yet ready to
    receive frames either. Same short-circuit.
    """
    ws = SimpleNamespace(
        application_state=WebSocketState.CONNECTING,
        send_text=AsyncMock(),
    )

    await _send_message(ws, _msg("PLAYER_PRESENCE"))

    ws.send_text.assert_not_awaited()
