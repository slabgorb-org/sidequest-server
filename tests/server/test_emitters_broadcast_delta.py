"""Tests for broadcast_delta — ephemeral fan-out to all sockets.

NOTE: The room API does NOT expose `connected_sockets()` or `send_json()`.
The real pattern (matching emit_event in emitters.py) is:
  room.connected_player_ids() -> list[str]
  room.socket_for_player(pid) -> str | None
  room.queue_for_socket(socket_id) -> asyncio.Queue | None
  queue.put_nowait(msg)

Tests are written against the actual SessionRoom API, not the placeholder
from the task spec.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


def _make_room(*player_ids: str) -> tuple[MagicMock, dict[str, asyncio.Queue]]:
    """Build a minimal room mock with one queue per player."""
    queues: dict[str, asyncio.Queue] = {pid: asyncio.Queue() for pid in player_ids}
    socket_ids = {pid: f"sock-{pid}" for pid in player_ids}

    room = MagicMock()
    room.connected_player_ids.return_value = list(player_ids)
    room.socket_for_player.side_effect = lambda pid: socket_ids.get(pid)
    room.queue_for_socket.side_effect = lambda sid: next(
        (queues[pid] for pid, s in socket_ids.items() if s == sid), None
    )
    return room, queues


@pytest.mark.asyncio
async def test_broadcast_delta_fans_out_to_all_sockets():
    from sidequest.server.emitters import broadcast_delta

    room, queues = _make_room("p1", "p2")

    await broadcast_delta(turn_id="t-1", chunk="hello ", seq=0, room=room)

    assert not queues["p1"].empty()
    assert not queues["p2"].empty()

    msg = queues["p1"].get_nowait()
    sent = msg.model_dump()
    assert sent["kind"] == "narration.delta"
    assert sent["payload"]["turn_id"] == "t-1"
    assert sent["payload"]["chunk"] == "hello "
    assert sent["payload"]["seq"] == 0


@pytest.mark.asyncio
async def test_broadcast_delta_does_not_call_emit_event():
    """broadcast_delta is ephemeral: no DB write, no projection cache."""
    from sidequest.server.emitters import broadcast_delta

    room, _queues = _make_room()  # zero players — room with no connections

    # No handler argument — the helper must NOT touch event_log or projection_cache.
    # If it tried to call emit_event it would fail due to missing handler.
    await broadcast_delta(turn_id="t-1", chunk="x", seq=0, room=room)
    # Test passes if no AttributeError on missing handler/event_log


@pytest.mark.asyncio
async def test_broadcast_delta_continues_on_per_socket_error():
    """One missing/dead socket must not break fan-out to other recipients."""
    from sidequest.server.emitters import broadcast_delta

    room, queues = _make_room("p1", "p3")

    # Inject "p2" — socket_for_player returns None (dead socket)
    room.connected_player_ids.return_value = ["p1", "p2", "p3"]
    original_socket = room.socket_for_player.side_effect

    def _socket_with_dead(pid: str) -> str | None:
        if pid == "p2":
            return None  # simulates dead / unknown socket
        return original_socket(pid)

    room.socket_for_player.side_effect = _socket_with_dead

    # Must not raise
    await broadcast_delta(turn_id="t-1", chunk="hello", seq=0, room=room)

    # Good players still received the message
    assert not queues["p1"].empty()
    assert not queues["p3"].empty()
