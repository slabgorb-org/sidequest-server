"""Story 61-followup-C — close_store() wiring on last disconnect.

Wiring test. The 61-4 baseline-reset machinery (``reset_baselines`` on
``AnthropicSdkClient``) is invoked from ``SessionRoom.close_store()``,
but ``close_store`` has no production callers until this story; the
``RoomRegistry`` never evicts rooms, so without a teardown trigger the
orchestrator's SDK client persists across multiple sessions on the same
slug with an unreset baseline — the cross-session-state hazard that
61-followup-A partly addressed by session-id-keying the baselines.

This story wires ``close_store()`` into the last-disconnect path inside
``ws_endpoint``. When a real player disconnect leaves the room with zero
connected players, ``close_store()`` must fire. Transient HMR
disconnects (multi-socket player, one socket closes) must NOT fire it.

Test strategy: drive a real ``ws_endpoint`` end-to-end via a minimal
fake handler / fake WebSocket pair. This is a fixture-driven behaviour
test per ``sidequest-server/CLAUDE.md`` "Every Test Suite Needs a Wiring
Test" — no source-text grep, no ``handler_path.read_text()``: we call
the real production endpoint and assert on the room's mock store.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState

from sidequest.game.persistence import GameMode
from sidequest.server.session_room import RoomRegistry, SessionRoom
from sidequest.server.websocket import ws_endpoint


def _fake_ws() -> SimpleNamespace:
    """A WebSocket that accepts, then disconnects cleanly on first read.

    Triggers the ``WebSocketDisconnect`` branch in ``ws_endpoint`` so
    control reaches the ``finally`` block where the close_store wiring
    lives.
    """

    ws = SimpleNamespace(
        client=("127.0.0.1", 54321),
        app=SimpleNamespace(state=SimpleNamespace(room_registry=RoomRegistry())),
        application_state=WebSocketState.CONNECTED,
        client_state=WebSocketState.CONNECTED,
    )

    async def accept() -> None:
        return None

    async def receive_text() -> str:
        raise WebSocketDisconnect(code=1000)

    async def close(code: int = 1000) -> None:  # noqa: ARG001
        ws.application_state = WebSocketState.DISCONNECTED

    async def send_text(_text: str) -> None:
        return None

    ws.accept = accept
    ws.receive_text = receive_text
    ws.close = close
    ws.send_text = send_text
    return ws


class _PinnedRoomHandler:
    """Minimal WebSocketSessionHandler that pins a pre-built room.

    ``ws_endpoint`` calls ``attach_room_context`` right after ``accept()``
    with the socket_id it generated. We use that hook to register the
    socket on our pre-built room, so the ``finally``-block
    ``room.disconnect(socket_id=...)`` actually finds and removes the
    player. ``current_room()`` then returns the same room for cleanup.
    """

    def __init__(self, room: SessionRoom, *, prewire: list[tuple[str, str]]) -> None:
        # prewire: pairs of (player_id, socket_id) to connect BEFORE
        # ws_endpoint runs. The endpoint's own socket_id is added on top
        # via attach_room_context.
        self._room = room
        self._captured_socket_id: str | None = None
        self._connect_player_for_endpoint: str | None = None
        self.cleanup_calls = 0
        for player_id, socket_id in prewire:
            room.connect(player_id, socket_id=socket_id)

    async def cleanup(self) -> None:
        """Mirror the production WebSocketSessionHandler.cleanup() contract:
        persist the canonical snapshot via room.save() before returning.

        Without this, the ordering bug caught by Architect spec-check
        2026-05-24 (close_store nulling _store before the final save)
        cannot be detected by these tests — an AsyncMock cleanup never
        reaches the save path, so the regression hides behind a green
        suite.
        """
        self.cleanup_calls += 1
        self._room.save()

    def bind_endpoint_socket_to(self, player_id: str) -> None:
        """Have attach_room_context register the endpoint's socket as
        belonging to ``player_id``. Without this the disconnect call
        in ws_endpoint finds no matching player.
        """
        self._connect_player_for_endpoint = player_id

    def attach_room_context(
        self,
        *,
        registry: RoomRegistry | None = None,
        socket_id: str,
        out_queue: Any,
    ) -> None:
        del registry  # not used by this test
        self._captured_socket_id = socket_id
        if self._connect_player_for_endpoint is not None:
            self._room.connect(self._connect_player_for_endpoint, socket_id=socket_id)
            self._room.attach_outbound(socket_id, out_queue)

    def current_room(self) -> SessionRoom | None:
        return self._room

    async def handle_message(self, _msg: Any) -> list[Any]:
        # Never reached; receive_text raises before any message arrives.
        return []


@pytest.mark.asyncio
async def test_ws_endpoint_calls_close_store_when_last_player_disconnects():
    """Wiring: solo room, single socket disconnects → room empty → close_store fires.

    This is the load-bearing assertion for the story: the production
    endpoint (``ws_endpoint``) is the only call site for
    ``room.close_store()`` after this change. Without the wiring, the
    rolling baseline survives across session reuse and the cost runaway
    alarm (61-4) silences itself over time.
    """
    room = SessionRoom(slug="slug-wire-solo", mode=GameMode.SOLO)
    store = MagicMock()
    room.bind_world(snapshot=MagicMock(), store=store)

    handler = _PinnedRoomHandler(room, prewire=[])
    handler.bind_endpoint_socket_to("alice")

    ws = _fake_ws()
    await ws_endpoint(ws, handler)

    assert handler._captured_socket_id is not None, (
        "attach_room_context must be called during the ws_endpoint lifecycle"
    )
    assert room.connected_player_ids() == [], (
        "After the only player's socket disconnects, the room should be empty"
    )
    assert store.close.call_count == 1, (
        "Wiring failure: ws_endpoint did not call room.close_store() "
        "when the last player disconnected. Expected exactly one close() "
        f"on the mock store, got {store.close.call_count}."
    )
    assert handler.cleanup_calls == 1, "handler.cleanup() must run exactly once"

    # Ordering guard (Architect spec-check finding 2026-05-24): the final
    # snapshot save in handler.cleanup() must run BEFORE close_store() nulls
    # _store, otherwise the final save silently no-ops on the now-None store
    # (session_room.py:277). All save and close calls flow through the same
    # MagicMock store, so its method_calls list preserves order.
    method_names = [c[0] for c in store.method_calls]
    assert "save" in method_names, (
        "handler.cleanup() must call room.save() (which calls store.save) "
        f"before teardown. Got method_calls={store.method_calls!r}"
    )
    assert "close" in method_names, "store.close must be called by close_store()"
    assert method_names.index("save") < method_names.index("close"), (
        "Ordering failure: store.close was called before store.save. "
        "close_store() must run AFTER handler.cleanup() persists the final "
        "snapshot — otherwise the final save silently no-ops. "
        f"Observed order: {method_names}"
    )


@pytest.mark.asyncio
async def test_ws_endpoint_does_not_close_store_on_transient_hmr_disconnect():
    """Wiring: multi-socket player, one socket closes → room still has
    that player on the surviving socket → close_store must NOT fire.

    Mirrors the Vite/HMR tab-reload pattern documented in
    ``session_room.connect()``: a single ``player_id`` can hold multiple
    sockets at once, and only the *last* socket's departure should
    teardown the room. Story 45-7 (pingpong 2026-05-07) added the
    multi-socket refcounting; this test guards the 61-followup-C
    teardown trigger against false positives on that path.
    """
    room = SessionRoom(slug="slug-wire-hmr", mode=GameMode.SOLO)
    store = MagicMock()
    room.bind_world(snapshot=MagicMock(), store=store)

    # Pre-wire one of alice's sockets (the HMR-survivor). The endpoint's
    # own socket becomes the second socket and is the one that
    # disconnects when receive_text() raises.
    handler = _PinnedRoomHandler(room, prewire=[("alice", "sock-survivor")])
    handler.bind_endpoint_socket_to("alice")

    ws = _fake_ws()
    await ws_endpoint(ws, handler)

    assert room.connected_player_ids() == ["alice"], (
        "Alice should still be connected on the surviving socket"
    )
    assert store.close.call_count == 0, (
        "Wiring failure: close_store() fired on a transient (multi-socket) "
        "disconnect. It must only fire when the room is fully empty."
    )
    assert handler.cleanup_calls == 1, "handler.cleanup() must run exactly once"


@pytest.mark.asyncio
async def test_ws_endpoint_does_not_close_store_on_mid_mp_disconnect():
    """Wiring: multiplayer room with two players, one leaves → room
    still has the other player → close_store must NOT fire.

    Solo-only teardown is not the contract: a multiplayer mid-game
    disconnect must not nuke the canonical store and reset baselines
    while another player is still playing. Only the *last* player's
    departure triggers teardown.
    """
    room = SessionRoom(slug="slug-wire-mp-mid", mode=GameMode.MULTIPLAYER)
    store = MagicMock()
    room.bind_world(snapshot=MagicMock(), store=store)

    # Pre-wire bob on his own socket. Alice's socket is the endpoint's,
    # bound via bind_endpoint_socket_to.
    handler = _PinnedRoomHandler(room, prewire=[("bob", "sock-bob")])
    handler.bind_endpoint_socket_to("alice")

    ws = _fake_ws()
    await ws_endpoint(ws, handler)

    assert room.connected_player_ids() == ["bob"], (
        "Bob should still be connected after Alice leaves"
    )
    assert store.close.call_count == 0, (
        "Wiring failure: close_store() fired while another player was "
        "still connected. MP mid-game teardown must be inhibited."
    )
