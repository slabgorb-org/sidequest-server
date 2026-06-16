"""End-to-end wiring test for ACTION_REVEAL.

Boots a real SessionRoom with two or three connected players, dispatches an
ACTION_REVEAL composing/submitted message from socket 1, and asserts that
peer sockets' outbound queues receive the message verbatim while the sender's
queue is empty.

Per CLAUDE.md "Verify Wiring, Not Just Existence" — this proves the
ACTION_REVEAL pipeline is reachable from production dispatch, that
SessionRoom.broadcast() fans out correctly, and that exclude_socket_id is
respected by real broadcast — not just MagicMock plumbing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from sidequest.game.persistence import GameMode, SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.handlers.action_reveal import ActionRevealHandler
from sidequest.protocol.messages import (
    ActionRevealMessage,
    ActionRevealPayload,
    ActionRevealStatus,
)
from sidequest.server.session_room import SessionRoom


def _make_bound_room(slug: str) -> SessionRoom:
    """Construct a SessionRoom bound to a minimal in-memory snapshot."""
    room = SessionRoom(slug=slug, mode=GameMode.MULTIPLAYER)
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="mawdeep")
    snap.turn_manager.round = 1
    store = SqliteStore.open_in_memory()
    room.bind_world(snapshot=snap, store=store)
    return room


def _make_session(room: SessionRoom, player_id: str, socket_id: str) -> MagicMock:
    """Minimal session mock — only the fields ActionRevealHandler reads."""
    session = MagicMock()
    session._socket_id = socket_id
    session._session_data.player_id = player_id
    session._room = room
    return session


def _make_msg(
    *,
    player_id: str = "p1",
    character_name: str = "Alex",
    status: ActionRevealStatus,
    action: str,
    seq: int = 0,
) -> ActionRevealMessage:
    payload = ActionRevealPayload(
        player_id=player_id,
        character_name=character_name,
        status=status,
        action=action,
        aside=False,
        seq=seq,
        round=0,
    )
    return ActionRevealMessage(payload=payload, player_id=player_id)


@pytest.mark.asyncio
async def test_composing_fans_out_to_peer_only() -> None:
    """ACTION_REVEAL composing from socket 1 lands on socket 2; not on socket 1."""
    room = _make_bound_room("wiring-test-2p")

    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()

    room.connect("p1", socket_id="s1")
    room.connect("p2", socket_id="s2")
    room.attach_outbound("s1", q1)
    room.attach_outbound("s2", q2)

    session = _make_session(room, player_id="p1", socket_id="s1")

    handler = ActionRevealHandler()
    msg = _make_msg(
        player_id="p1",
        status=ActionRevealStatus.COMPOSING,
        action="I sneak around the back",
        seq=0,
    )

    result = await handler.handle(session, msg)
    assert result == []

    assert q1.empty(), "sender's own socket must not receive its own composing"
    assert not q2.empty(), "peer socket must receive the broadcast"

    received = q2.get_nowait()
    assert isinstance(received, ActionRevealMessage)
    assert received.payload.action == "I sneak around the back"
    # Server stamps player_id authoritatively from session._session_data.player_id.
    assert str(received.payload.player_id) == "p1"
    assert received.payload.status == ActionRevealStatus.COMPOSING


@pytest.mark.asyncio
async def test_three_player_fanout_excludes_only_sender() -> None:
    """With 3 sockets, sender's queue empty; both peers receive."""
    room = _make_bound_room("wiring-test-3p")

    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    q3: asyncio.Queue = asyncio.Queue()

    room.connect("p1", socket_id="s1")
    room.connect("p2", socket_id="s2")
    room.connect("p3", socket_id="s3")
    room.attach_outbound("s1", q1)
    room.attach_outbound("s2", q2)
    room.attach_outbound("s3", q3)

    session = _make_session(room, player_id="p1", socket_id="s1")

    handler = ActionRevealHandler()
    msg = _make_msg(
        player_id="p1",
        status=ActionRevealStatus.SUBMITTED,
        action="I draw my pistol",
        seq=1,
    )

    result = await handler.handle(session, msg)
    assert result == []

    assert q1.empty(), "sender's queue must be empty"
    assert not q2.empty(), "peer 2 must receive the broadcast"
    assert not q3.empty(), "peer 3 must receive the broadcast"

    received_2 = q2.get_nowait()
    received_3 = q3.get_nowait()
    assert isinstance(received_2, ActionRevealMessage)
    assert isinstance(received_3, ActionRevealMessage)
    assert received_2.payload.action == "I draw my pistol"
    assert received_3.payload.action == "I draw my pistol"
    assert received_2.payload.status == ActionRevealStatus.SUBMITTED
    assert received_3.payload.status == ActionRevealStatus.SUBMITTED
    # Server stamps player_id from session, not client payload.
    assert str(received_2.payload.player_id) == "p1"
    assert str(received_3.payload.player_id) == "p1"
