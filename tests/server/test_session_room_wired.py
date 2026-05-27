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
)
from sidequest.protocol import GameMessage
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
_SLUG = "room-wired-fixture"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: connect resolves the bootstrap row from Postgres)."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _seed_game(save_dir: Path, slug: str) -> None:
    """Register an empty MP session in Postgres (ADR-115 F1)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )


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
