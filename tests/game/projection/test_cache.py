"""ProjectionCache — per-player decision cache backed by Postgres (ADR-115 F1)."""

from __future__ import annotations

import pytest

from sidequest.game.projection.cache import CachedDecision, ProjectionCache
from sidequest.game.projection_filter import FilterDecision


@pytest.fixture
def repo(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """A real PgSaveRepository on a per-worker throwaway PG db.

    TRUNCATE … RESTART IDENTITY per test means appended events get seqs
    starting at 1 (the projection_cache FK target), matching the prior
    per-test in-memory SqliteStore.
    """
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
    r, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug="projection-cache",
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )
    try:
        yield r
    finally:
        db_pool.close_pool()


def _insert_event(repo, kind: str = "NARRATION", payload: str = "{}") -> int:
    """Append an event so the projection_cache FK is satisfied; return its seq."""
    return repo.append_event(kind=kind, payload_json=payload).seq


def test_write_and_read_single_row(repo) -> None:
    cache = ProjectionCache(repo)
    seq = _insert_event(repo)
    dec = FilterDecision(include=True, payload_json='{"text":"hi"}')
    cache.write(event_seq=seq, player_id="alice", decision=dec)
    rows = cache.read_since(player_id="alice", since_seq=0)
    assert rows == [CachedDecision(event_seq=seq, include=True, payload_json='{"text":"hi"}')]


def test_read_since_filters_by_seq(repo) -> None:
    cache = ProjectionCache(repo)
    s1 = _insert_event(repo)
    s2 = _insert_event(repo)
    s3 = _insert_event(repo)
    cache.write(event_seq=s1, player_id="alice", decision=FilterDecision(True, '{"a":1}'))
    cache.write(event_seq=s2, player_id="alice", decision=FilterDecision(True, '{"a":2}'))
    cache.write(event_seq=s3, player_id="alice", decision=FilterDecision(True, '{"a":3}'))
    rows = cache.read_since(player_id="alice", since_seq=s1)
    assert [r.event_seq for r in rows] == [s2, s3]


def test_omitted_decision_stores_none_payload(repo) -> None:
    cache = ProjectionCache(repo)
    seq = _insert_event(repo)
    cache.write(event_seq=seq, player_id="alice", decision=FilterDecision(False, ""))
    rows = cache.read_since(player_id="alice", since_seq=0)
    assert rows[0].include is False
    assert rows[0].payload_json is None


def test_multiple_players_isolated(repo) -> None:
    cache = ProjectionCache(repo)
    seq = _insert_event(repo)
    cache.write(event_seq=seq, player_id="alice", decision=FilterDecision(True, '{"who":"alice"}'))
    cache.write(event_seq=seq, player_id="bob", decision=FilterDecision(False, ""))
    assert cache.read_since(player_id="alice", since_seq=0)[0].payload_json == '{"who":"alice"}'
    assert cache.read_since(player_id="bob", since_seq=0)[0].include is False


def test_duplicate_write_is_idempotent_by_primary_key(repo) -> None:
    cache = ProjectionCache(repo)
    seq = _insert_event(repo)
    cache.write(event_seq=seq, player_id="alice", decision=FilterDecision(True, '{"v":1}'))
    cache.write(event_seq=seq, player_id="alice", decision=FilterDecision(True, '{"v":2}'))
    rows = cache.read_since(player_id="alice", since_seq=0)
    assert rows == [CachedDecision(event_seq=seq, include=True, payload_json='{"v":2}')]
