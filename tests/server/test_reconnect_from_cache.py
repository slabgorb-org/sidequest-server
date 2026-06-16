"""Reconnect reads pre-computed projection_cache — bit-identical to live frames."""

from __future__ import annotations

import pytest

from sidequest.game.event_log import EventLog
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import SessionGameStateView
from sidequest.game.projection_filter import FilterDecision


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
        db_pool.get_pool(),
        slug="reconnect-cache",
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )
    try:
        yield repo
    finally:
        db_pool.close_pool()


def test_reconnect_replays_cached_payloads(pg_repo) -> None:
    repo = pg_repo
    log = EventLog(repo)
    cache = ProjectionCache(repo)
    filt = ComposedFilter.with_no_genre_rules()
    view = SessionGameStateView(
        player_id_to_character={"alice": "alice_char"},
    )

    live_frames: dict[int, FilterDecision] = {}
    for text in ["one", "two", "three"]:
        row = log.append(kind="NARRATION", payload_json=f'{{"text":"{text}"}}')
        env = MessageEnvelope(kind=row.kind, payload_json=row.payload_json, origin_seq=row.seq)
        decision = filt.project(envelope=env, view=view, player_id="alice")
        cache.write(event_seq=row.seq, player_id="alice", decision=decision)
        live_frames[row.seq] = decision

    replayed = cache.read_since(player_id="alice", since_seq=0)
    assert len(replayed) == 3
    for cached in replayed:
        live = live_frames[cached.event_seq]
        assert cached.include == live.include
        assert cached.payload_json == (live.payload_json if live.include else None)
