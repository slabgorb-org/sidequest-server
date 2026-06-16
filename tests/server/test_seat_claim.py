"""PLAYER_SEAT → SEAT_CONFIRMED via direct handler dispatch (MP-02 Task 5).

The original test drove this through a real FastAPI app + TestClient +
websocket_connect. The only thing that added over the test body itself
was minutes of startup time; none of the assertions here need HTTP/WS
transport. ``WebSocketSessionHandler.handle_message`` already returns
the outbound list (``SEAT_CONFIRMED`` for the claimer) and the room
broadcast lands on the attached out-queue.
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
from sidequest.protocol.enums import MessageType
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
_SLUG = "seat-claim-fixture"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


def _seed(tmp_path: Path, slug: str) -> None:
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.MULTIPLAYER,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    store.close()


@pytest.mark.asyncio
async def test_player_seat_claim_broadcasts_seat_confirmed(tmp_path: Path) -> None:
    _seed(tmp_path, _SLUG)
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
    connect_out = await handler.handle_message(connect)
    assert connect_out, "connect must produce at least SESSION_CONNECTED"

    seat = GameMessage.model_validate(
        {
            "type": "PLAYER_SEAT",
            "player_id": "alice",
            "payload": {"character_slot": "rux"},
        }
    )
    seat_out = await handler.handle_message(seat)

    confirmed = [m for m in seat_out if getattr(m, "type", None) == MessageType.SEAT_CONFIRMED]
    if not confirmed:
        # Broadcast path: SEAT_CONFIRMED may land on the outbound queue
        # rather than the method return value (room.broadcast semantics).
        queued: list[object] = []
        while not out_queue.empty():
            queued.append(out_queue.get_nowait())
        confirmed = [m for m in queued if getattr(m, "type", None) == MessageType.SEAT_CONFIRMED]

    assert confirmed, (
        f"PLAYER_SEAT must produce a SEAT_CONFIRMED (via handler return or "
        f"room broadcast); got seat_out={seat_out}"
    )
    msg = confirmed[0]
    assert msg.payload.player_id == "alice"
    assert msg.payload.character_slot == "rux"
