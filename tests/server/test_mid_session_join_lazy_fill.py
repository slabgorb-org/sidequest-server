"""Mid-session join: lazy-fill cache for the new player."""

from __future__ import annotations

import pytest

from sidequest.game.event_log import EventLog
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.cache_fill import lazy_fill
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.view import SessionGameStateView
from sidequest.game.projection_filter import FilterDecision


@pytest.fixture
def pg_repo(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """A real PgSaveRepository on a per-worker throwaway PG db (ADR-115 F1).

    TRUNCATE … RESTART IDENTITY per test means each test sees event seqs
    starting at 1, matching the prior per-file in-memory SqliteStore.
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
    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug="lazy-fill",
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )
    try:
        yield repo
    finally:
        db_pool.close_pool()


def test_lazy_fill_populates_cache_for_new_player(pg_repo) -> None:
    repo = pg_repo
    log = EventLog(repo)
    cache = ProjectionCache(repo)
    filt = ComposedFilter.with_no_genre_rules()
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )

    log.append(kind="NARRATION", payload_json='{"text":"one"}')
    log.append(kind="NARRATION", payload_json='{"text":"two"}')

    filled = lazy_fill(
        event_log=log,
        cache=cache,
        filter_=filt,
        view=view,
        player_id="alice",
    )
    assert filled == 2

    rows = cache.read_since(player_id="alice", since_seq=0)
    assert [r.event_seq for r in rows] == [1, 2]


def test_lazy_fill_skips_already_cached_events(pg_repo) -> None:
    repo = pg_repo
    log = EventLog(repo)
    cache = ProjectionCache(repo)
    filt = ComposedFilter.with_no_genre_rules()
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )

    log.append(kind="NARRATION", payload_json='{"text":"one"}')
    log.append(kind="NARRATION", payload_json='{"text":"two"}')

    cache.write(
        event_seq=1,
        player_id="alice",
        decision=FilterDecision(include=True, payload_json='{"text":"one"}'),
    )

    filled = lazy_fill(event_log=log, cache=cache, filter_=filt, view=view, player_id="alice")
    assert filled == 1
