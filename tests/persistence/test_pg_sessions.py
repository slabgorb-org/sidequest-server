"""Session lifecycle + per-session row-lock transaction (ADR-115)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg._conn import session_tx


@pytest.fixture
def pool(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield db_pool.get_pool()
    db_pool.close_pool()


def test_ensure_session_inserts_then_returns_same_id(pool) -> None:
    sid1 = sessions.ensure_session(pool, slug="g_w", mode="solo", genre_slug="g", world_slug="w")
    sid2 = sessions.ensure_session(pool, slug="g_w", mode="solo", genre_slug="g", world_slug="w")
    assert sid1 == sid2  # upsert on session_slug, not a second row


def test_resolve_session_id_returns_none_for_unknown(pool) -> None:
    assert sessions.resolve_session_id(pool, slug="missing") is None


def test_get_game_roundtrips(pool) -> None:
    sessions.ensure_session(pool, slug="g_w", mode="multiplayer", genre_slug="g", world_slug="w")
    row = sessions.get_game(pool, slug="g_w")
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
    sid = sessions.ensure_session(pool, slug="g_w", mode="solo", genre_slug="g", world_slug="w")
    with session_tx(pool, sid) as conn:
        _insert_narrative_row(conn, sid)
    # Fresh connection sees the committed row.
    assert _narrative_count(pool, sid) == 1


def test_session_tx_rolls_back_on_exception(pool) -> None:
    sid = sessions.ensure_session(pool, slug="g_w", mode="solo", genre_slug="g", world_slug="w")
    with pytest.raises(RuntimeError), session_tx(pool, sid) as conn:
        _insert_narrative_row(conn, sid)
        raise RuntimeError("boom")
    # Fresh connection: the row was rolled back.
    assert _narrative_count(pool, sid) == 0
