import pytest

from sidequest.game.event_log import EventLog


@pytest.fixture
def store(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """A real PgSaveRepository on a per-worker throwaway PG db (ADR-115 F1).

    TRUNCATE … RESTART IDENTITY per test means event seqs start at 1, matching
    the prior fresh-per-test in-memory SqliteStore.
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
        slug="2026-04-22-moldharrow-keep",
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )
    try:
        yield repo
    finally:
        db_pool.close_pool()


def test_append_assigns_monotonic_seq(store):
    log = EventLog(store)
    r1 = log.append(kind="NARRATION", payload_json='{"text":"hello"}')
    r2 = log.append(kind="STATE_UPDATE", payload_json='{"hp":10}')
    assert r1.seq == 1
    assert r2.seq == 2


def test_read_since_returns_only_newer(store):
    log = EventLog(store)
    for i in range(5):
        log.append(kind="NARRATION", payload_json=f'{{"i":{i}}}')
    rows = log.read_since(since_seq=2)
    assert [r.seq for r in rows] == [3, 4, 5]


def test_read_since_zero_returns_all(store):
    log = EventLog(store)
    log.append(kind="NARRATION", payload_json='{"i":1}')
    log.append(kind="NARRATION", payload_json='{"i":2}')
    rows = log.read_since(since_seq=0)
    assert len(rows) == 2


def test_latest_seq(store):
    log = EventLog(store)
    assert log.latest_seq() == 0
    log.append(kind="NARRATION", payload_json="{}")
    log.append(kind="STATE_UPDATE", payload_json="{}")
    assert log.latest_seq() == 2
