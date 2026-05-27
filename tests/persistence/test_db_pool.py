"""ConnectionPool foundation — single source, lazy, fail-loud (ADR-115)."""

from __future__ import annotations

import pytest

from sidequest.game import db_pool


def test_get_pool_uses_database_url(monkeypatch, migrated_db: str) -> None:
    # migrated_db is the +psycopg form; the pool wants the plain conninfo.
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()  # reset any cached pool
    pool = db_pool.get_pool()
    with pool.connection() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    db_pool.close_pool()


def test_get_pool_is_singleton(monkeypatch, migrated_db: str) -> None:
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    assert db_pool.get_pool() is db_pool.get_pool()
    db_pool.close_pool()


def test_get_pool_fails_loud_when_url_unset(monkeypatch) -> None:
    monkeypatch.delenv("SIDEQUEST_DATABASE_URL", raising=False)
    db_pool.close_pool()
    from sidequest.game.db_config import MissingDatabaseUrlError

    try:
        with pytest.raises(MissingDatabaseUrlError):
            db_pool.get_pool()
    finally:
        db_pool.close_pool()
