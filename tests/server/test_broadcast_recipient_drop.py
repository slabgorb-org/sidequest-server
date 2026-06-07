"""``SessionRoom.broadcast`` must surface a connected-but-undeliverable
recipient LOUDLY — the silent skip is the exact blind spot that stranded a
sealed peer at "Composing" (Story 67-2).

The seal-presence frames (ACTION_REVEAL{submitted}, TURN_STATUS{submitted})
ride ``SessionRoom.broadcast`` (session_room.py:865) — a *different* path from
the event-sourced ``emitters._deliver_fanout`` that 67-1 already instrumented
with ``broadcast.recipient_dropped``. ``broadcast`` iterates only
``_outbound_queues``; a player who is in ``_connected`` (an intended recipient)
but whose socket has no registered queue — the precise transient state a
mid-turn socket churn leaves behind — is skipped with no log and no watcher
event. The GM panel then sees a clean broadcast while a player received
nothing. Per CLAUDE.md (No Silent Fallbacks + OTEL lie-detector) that drop must
emit a watcher event, mirroring 67-1's ``broadcast.recipient_dropped``.

These tests drive the REAL ``SessionRoom.broadcast`` against a real
``RoomRegistry`` with a real ``TurnStatusMessage`` — no genre pack, no DB.
"""

from __future__ import annotations

import asyncio

import pytest

from sidequest.game.persistence import GameMode
from sidequest.protocol.messages import TurnStatusMessage, TurnStatusPayload
from sidequest.protocol.types import NonBlankString
from sidequest.server import session_room as session_room_module
from sidequest.server.session_room import RoomRegistry


def _turn_status_msg() -> TurnStatusMessage:
    """A seal-presence frame of the kind that strands a peer when dropped."""
    return TurnStatusMessage(
        payload=TurnStatusPayload(
            player_name=NonBlankString("Adam"),
            status="submitted",
        ),
        player_id="adam",
    )


def _spy_watcher(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict, dict]]:
    """Spy ``session_room._watcher_publish`` (imported at module load,
    session_room.py:30). Returns the recording list."""
    published: list[tuple[str, dict, dict]] = []

    def _record(event_type, fields, **kwargs):
        published.append((event_type, dict(fields), dict(kwargs)))

    monkeypatch.setattr(session_room_module, "_watcher_publish", _record)
    return published


def _drops(published: list[tuple[str, dict, dict]]) -> list[tuple[dict, dict]]:
    return [
        (fields, kwargs)
        for (_event_type, fields, kwargs) in published
        if fields.get("field") == "broadcast.recipient_dropped"
    ]


def test_connected_recipient_without_queue_emits_loud_watcher_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eve is connected (intended recipient) but her socket has no outbound
    queue — the churn state. Broadcasting a seal frame must fire a
    ``broadcast.recipient_dropped`` watcher at WARNING severity naming Eve,
    instead of silently dropping her TURN_STATUS."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug="seal-drop-test", mode=GameMode.MULTIPLAYER)
    room.connect("adam", socket_id="sock-adam")
    room.connect("eve", socket_id="sock-eve")
    room.attach_outbound("sock-adam", asyncio.Queue())
    # NOTE: deliberately NO attach_outbound for sock-eve — her socket churned.
    published = _spy_watcher(monkeypatch)

    room.broadcast(_turn_status_msg())

    drops = _drops(published)
    assert drops, (
        "a connected recipient whose socket has no outbound queue must emit a "
        "broadcast.recipient_dropped watcher event — a silent skip is exactly "
        "what stranded the peer at 'Composing' (67-2)"
    )
    fields, kwargs = drops[0]
    assert fields["recipient_player_id"] == "eve"
    assert fields["type"] == "TURN_STATUS", "the dropped frame's type must be carried for the GM panel"
    assert kwargs.get("severity") == "warning"
    assert kwargs.get("component") in {"multiplayer", "broadcast"}


def test_live_recipient_still_receives_frame_despite_peer_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eve's drop must not starve Adam: his live queue still receives the
    seal frame. Recovery must never reduce delivery to healthy peers."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug="seal-drop-live", mode=GameMode.MULTIPLAYER)
    room.connect("adam", socket_id="sock-adam")
    room.connect("eve", socket_id="sock-eve")
    adam_q: asyncio.Queue = asyncio.Queue()
    room.attach_outbound("sock-adam", adam_q)
    _spy_watcher(monkeypatch)

    room.broadcast(_turn_status_msg())

    assert adam_q.qsize() == 1, "the live recipient must still receive the broadcast frame"


def test_fully_disconnected_player_is_not_a_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A player who is NOT in ``_connected`` (cleanly disconnected, no socket)
    is not an intended recipient — broadcasting must NOT report them as a
    dropped recipient. Only a connected-but-queueless socket is a real drop."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug="seal-drop-clean", mode=GameMode.MULTIPLAYER)
    room.connect("adam", socket_id="sock-adam")
    room.attach_outbound("sock-adam", asyncio.Queue())
    # eve never connected — not an intended recipient.
    published = _spy_watcher(monkeypatch)

    room.broadcast(_turn_status_msg())

    assert not _drops(published), (
        "a player who is not connected is not a mid-broadcast drop — only a "
        "connected recipient with a missing queue counts"
    )
