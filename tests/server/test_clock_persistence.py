"""Round-trip test for GameSnapshot.clock_t_hours.

Verifies the new field rides on the save/load path (ADR-115 F1: Postgres)
without schema migrations. Old saves without the field load with default 0.0.
"""

from __future__ import annotations

import pytest

from sidequest.game.session import GameSnapshot


@pytest.fixture
def pg_repo(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """A real PgSaveRepository on a per-worker throwaway PG db (ADR-115 F1)."""
    import psycopg

    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

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
    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(), slug="clock-roundtrip", mode="solo", genre_slug="", world_slug=""
    )
    try:
        yield repo
    finally:
        db_pool.close_pool()


def test_clock_t_hours_round_trip(pg_repo):
    """Save a GameSnapshot with non-zero clock_t_hours, load, verify."""
    snap = GameSnapshot(clock_t_hours=42.0)
    pg_repo.init_session()
    pg_repo.save(snap)

    loaded = pg_repo.load()
    assert loaded is not None
    assert loaded.snapshot.clock_t_hours == 42.0


def test_clock_t_hours_default_zero():
    """Default value when not set."""
    snap = GameSnapshot()
    assert snap.clock_t_hours == 0.0


def test_clock_t_hours_preserved_through_dict_roundtrip():
    """Pydantic model_dump / model_validate round trip preserves field."""
    snap = GameSnapshot(clock_t_hours=17.5)
    data = snap.model_dump()
    assert data["clock_t_hours"] == 17.5
    restored = GameSnapshot.model_validate(data)
    assert restored.clock_t_hours == 17.5
