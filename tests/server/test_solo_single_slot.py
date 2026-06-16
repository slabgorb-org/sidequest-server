"""Solo game slug rejects second connection (via direct handler dispatch).

Two ``WebSocketSessionHandler`` instances sharing a ``RoomRegistry``
stand in for the two websockets. Second handler's connect must yield
an ERROR with 'solo' in the message. No FastAPI, no TestClient.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.persistence import (
    GameMode,
)
from sidequest.protocol import GameMessage
from sidequest.protocol.enums import MessageType
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
_SLUG = "solo-slot-fixture"
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


def _seed_solo(tmp_path: Path, slug: str) -> None:
    """Register an empty SOLO session in Postgres (ADR-115 F1)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )


def _make_handler(
    tmp_path: Path,
    registry: RoomRegistry,
    socket_id: str,
) -> tuple[WebSocketSessionHandler, asyncio.Queue[object]]:
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=registry,
        socket_id=socket_id,
        out_queue=queue,
    )
    return handler, queue


@pytest.mark.asyncio
async def test_second_connection_to_solo_is_rejected(tmp_path: Path) -> None:
    _seed_solo(tmp_path, _SLUG)
    registry = RoomRegistry()

    alice, _alice_q = _make_handler(tmp_path, registry, "sock-alice")
    await alice.handle_message(
        GameMessage.model_validate(
            {
                "type": "SESSION_EVENT",
                "player_id": "alice",
                "payload": {"event": "connect", "game_slug": _SLUG},
            }
        )
    )

    bob, _bob_q = _make_handler(tmp_path, registry, "sock-bob")
    bob_out = await bob.handle_message(
        GameMessage.model_validate(
            {
                "type": "SESSION_EVENT",
                "player_id": "bob",
                "payload": {"event": "connect", "game_slug": _SLUG},
            }
        )
    )

    errors = [m for m in bob_out if getattr(m, "type", None) == MessageType.ERROR]
    assert errors, f"bob's connect to a SOLO room must produce ERROR; got {bob_out}"
    msg = str(errors[0].payload.message).lower()
    assert "solo" in msg, f"expected 'solo' in error; got {msg!r}"
