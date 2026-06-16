"""Session lifecycle + per-session row-lock transaction (ADR-115).

Slug isolation
--------------
``migrated_db`` is session-scoped per xdist worker, so every test on a worker
shares ONE database and the ``sessions`` rows written via the (committing)
``pool`` fixture PERSIST across tests in that worker. Slugs MUST therefore be
uuid-namespaced per test (matching the A2-A6 store test pattern) — a fixed slug
like ``g_w`` collides across tests, and because ``ensure_session`` upserts
``ON CONFLICT (session_slug) DO UPDATE SET last_played`` (it does NOT rewrite
``mode``), a prior ``solo`` insert would leave a later ``multiplayer``
``ensure_session`` a no-op on mode and fail the roundtrip assertion in whatever
worker order xdist happened to choose.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg._conn import session_tx


def _slug() -> str:
    return f"g_w_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def pool(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield db_pool.get_pool()
    db_pool.close_pool()


def test_ensure_session_inserts_then_returns_same_id(pool) -> None:
    slug = _slug()
    sid1 = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    sid2 = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    assert sid1 == sid2  # upsert on session_slug, not a second row


def test_resolve_session_id_returns_none_for_unknown(pool) -> None:
    assert sessions.resolve_session_id(pool, slug=f"missing_{uuid.uuid4().hex[:8]}") is None


def test_get_game_roundtrips(pool) -> None:
    slug = _slug()
    sessions.ensure_session(pool, slug=slug, mode="multiplayer", genre_slug="g", world_slug="w")
    row = sessions.get_game(pool, slug=slug)
    assert row is not None
    assert row.mode == "multiplayer"
    assert row.genre_slug == "g"
    assert row.world_slug == "w"
    assert row.claude_session_id is None


def _insert_narrative_row(conn, session_id: int) -> None:
    conn.execute(
        "INSERT INTO narrative_log (session_id, round_number, author, content, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (session_id, 1, "narrator", "a beat", datetime.now(tz=UTC).isoformat()),
    )


def _narrative_count(pool, session_id: int) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM narrative_log WHERE session_id = %s", (session_id,)
        ).fetchone()
    return int(row[0])


def test_session_tx_commits_on_clean_exit(pool) -> None:
    sid = sessions.ensure_session(pool, slug=_slug(), mode="solo", genre_slug="g", world_slug="w")
    with session_tx(pool, sid) as conn:
        _insert_narrative_row(conn, sid)
    # Fresh connection sees the committed row.
    assert _narrative_count(pool, sid) == 1


def test_session_tx_rolls_back_on_exception(pool) -> None:
    sid = sessions.ensure_session(pool, slug=_slug(), mode="solo", genre_slug="g", world_slug="w")
    with pytest.raises(RuntimeError), session_tx(pool, sid) as conn:
        _insert_narrative_row(conn, sid)
        raise RuntimeError("boom")
    # Fresh connection: the row was rolled back.
    assert _narrative_count(pool, sid) == 0
