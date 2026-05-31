"""Ungraceful client-drop teardown in ``ws_endpoint``.

Playtest 2026-05-17 [BS-BUG-LOW] (firehose-reframed): a routine
ungraceful client disconnect surfaced as ``ws.unexpected_error`` + a
full ``RuntimeError`` traceback at **~1712×** in the live log, burying
every genuine ``ws.unexpected_error`` behind expected-event noise.

Root cause: ``ws_endpoint`` already catches ``WebSocketDisconnect``
(the graceful close path), but when the peer drops *ungracefully* and a
concurrent writer ``send`` has already advanced ``application_state``
past ``CONNECTED``, the reader loop's next ``websocket.receive_text()``
hits Starlette's top-of-function not-connected guard and raises a bare
``RuntimeError('WebSocket is not connected. Need to call "accept"
first.')``. That fell through to the generic ``except Exception``
catch-all → ``logger.exception("ws.unexpected_error")``.

Contract: an ungraceful drop is an *expected* teardown — log INFO, run
the same cleanup as ``WebSocketDisconnect``, emit no error frame, no
traceback. A ``RuntimeError`` raised while the socket is still
``CONNECTED`` is a genuine fault and MUST still surface loudly
(No Silent Fallbacks — the discriminator is the socket state Starlette
itself checks, never the exception message string).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from sidequest.server.websocket import ws_endpoint

_NOT_CONNECTED_MSG = 'WebSocket is not connected. Need to call "accept" first.'


class _FakeHandler:
    """Stand-in for ``WebSocketSessionHandler`` — ``ws_endpoint`` only
    touches ``attach_room_context``, ``current_room`` and ``cleanup``
    on the no-room teardown path this test exercises."""

    def __init__(self) -> None:
        self.attached = False
        self.cleanup = AsyncMock()

    def attach_room_context(self, **_kwargs: Any) -> None:
        self.attached = True

    def current_room(self) -> None:
        # None ⇒ ws_endpoint skips the presence-broadcast block and
        # goes straight to handler.cleanup() in the finally clause.
        return None


def _fake_ws(*, state_after_drop: WebSocketState) -> SimpleNamespace:
    """A WebSocket whose first ``receive_text()`` reproduces the
    production ungraceful-drop raise: a bare ``RuntimeError`` while
    ``application_state`` is ``state_after_drop``.

    ``state_after_drop=DISCONNECTED`` models the real bug (peer gone,
    Starlette's guard fired). ``CONNECTED`` models a genuine fault that
    must NOT be swallowed.
    """
    sent: list[str] = []
    ws = SimpleNamespace(
        client=("127.0.0.1", 54321),
        application_state=WebSocketState.CONNECTED,
        client_state=WebSocketState.CONNECTED,
        app=SimpleNamespace(state=SimpleNamespace(room_registry=object())),
        headers={"host": "localhost"},
        sent=sent,
    )

    async def accept() -> None:
        return None

    async def receive_text() -> str:
        ws.application_state = state_after_drop
        if state_after_drop == WebSocketState.DISCONNECTED:
            ws.client_state = WebSocketState.DISCONNECTED
        raise RuntimeError(_NOT_CONNECTED_MSG)

    async def send_text(data: str) -> None:
        sent.append(data)

    async def close(code: int = 1000) -> None:
        return None

    ws.accept = accept
    ws.receive_text = receive_text
    ws.send_text = send_text
    ws.close = close
    return ws


@pytest.mark.asyncio
async def test_ungraceful_drop_is_clean_info_teardown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The headline bug: an ungraceful drop must NOT log
    ``ws.unexpected_error`` / a traceback and must NOT push an error
    frame onto the dead socket — it is an expected teardown."""
    ws = _fake_ws(state_after_drop=WebSocketState.DISCONNECTED)
    handler = _FakeHandler()

    with caplog.at_level("INFO", logger="sidequest.server.websocket"):
        await ws_endpoint(ws, handler)  # type: ignore[arg-type]

    messages = [r.getMessage() for r in caplog.records]
    # No unexpected-error signature — this is the 1712× firehose line.
    assert not any("ws.unexpected_error" in m for m in messages), messages
    # No ERROR-level record (logger.exception logs at ERROR with a trace).
    assert not any(r.levelname == "ERROR" for r in caplog.records), [
        (r.levelname, r.getMessage()) for r in caplog.records
    ]
    # A clean INFO teardown breadcrumb was emitted instead.
    assert any("ws.disconnected" in m for m in messages), (
        f"expected an INFO ws.disconnected* breadcrumb, got {messages}"
    )
    # No error frame was shoved at the already-dead socket.
    assert ws.sent == [], ws.sent
    # Cleanup still ran — teardown is complete, not aborted.
    handler.cleanup.assert_awaited_once()
    # The normal end-of-lifecycle marker still fires.
    assert any("ws.session_cleanup_complete" in m for m in messages)


@pytest.mark.asyncio
async def test_runtime_error_while_connected_still_surfaces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silent-fallback guard: a ``RuntimeError`` raised while the socket
    is still ``CONNECTED`` is a genuine fault — it MUST still surface as
    ``ws.unexpected_error`` (the fix discriminates on socket state, it
    does not blanket-swallow every RuntimeError)."""
    ws = _fake_ws(state_after_drop=WebSocketState.CONNECTED)
    handler = _FakeHandler()

    with caplog.at_level("INFO", logger="sidequest.server.websocket"):
        await ws_endpoint(ws, handler)  # type: ignore[arg-type]

    messages = [r.getMessage() for r in caplog.records]
    assert any("ws.unexpected_error" in m for m in messages), (
        f"a still-CONNECTED RuntimeError must surface loudly, got {messages}"
    )
    # And it still surfaces a typed error frame to the (live) client.
    assert ws.sent, "expected an error frame on the still-connected socket"
    handler.cleanup.assert_awaited_once()


def test_ws_endpoint_is_the_production_ws_route() -> None:
    """Wiring: the function these tests drive is the exact one the
    FastAPI ``/ws`` route dispatches to in production (No half-wired
    test — imported AND reachable from the real app)."""
    from sidequest.server import app as app_module

    # app.py imports the real symbol …
    assert app_module.ws_endpoint is ws_endpoint
    # … and calls it inside the @app.websocket("/ws") route handler.
    src = inspect.getsource(app_module.create_app)
    assert '@app.websocket("/ws")' in src
    assert "await ws_endpoint(websocket, handler)" in src
