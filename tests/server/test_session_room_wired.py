"""Wiring test: RoomRegistry + WebSocketSessionHandler join/leave lifecycle.

Exercises the handler directly with a fake outbound queue — no FastAPI app,
no TestClient, no websocket_connect. The point of the test is that a
slug-connect adds the player to the room and the cleanup path removes
them; none of that needs HTTP transport.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.protocol import GameMessage
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
_SLUG = "room-wired-fixture"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


def _seed_game(save_dir: Path, slug: str) -> None:
    db = db_path_for_slug(save_dir, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode("multiplayer"),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    store.close()


@pytest.mark.asyncio
async def test_slug_connect_adds_player_and_cleanup_removes_them(
    tmp_path: Path,
) -> None:
    _seed_game(tmp_path, _SLUG)
    registry = RoomRegistry()
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    out_queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=registry,
        socket_id="sock-alice",
        out_queue=out_queue,
    )

    connect = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "alice",
            "payload": {"event": "connect", "game_slug": _SLUG},
        }
    )
    await handler.handle_message(connect)

    room = registry.get(_SLUG)
    assert room is not None, "room must exist after slug-connect"
    assert "alice" in room.connected_player_ids(), (
        f"alice must appear in room.connected_player_ids(); got {room.connected_player_ids()}"
    )

    # Simulate the ws_endpoint finally block: detach + disconnect + cleanup.
    room.detach_outbound("sock-alice")
    room.disconnect(socket_id="sock-alice")
    await handler.cleanup()

    room_after = registry.get(_SLUG)
    assert room_after is not None, "room must survive individual disconnect"
    assert "alice" not in room_after.connected_player_ids(), (
        f"alice must be removed on disconnect; got {room_after.connected_player_ids()}"
    )
