"""Snapshot (game_state) + world_save adapter over Postgres (ADR-115 A4)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from sidequest.game import db_pool
from sidequest.game.persistence import SaveSchemaIncompatibleError
from sidequest.game.pg import sessions
from sidequest.game.pg.snapshot import PgSnapshotStore
from sidequest.game.session import GameSnapshot
from sidequest.game.world_save import WorldSave


def _unique_slug() -> str:
    return f"g_w_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def store(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = _unique_slug()
    sid = sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="test_genre", world_slug="test_world"
    )
    yield PgSnapshotStore(pool, session_id=sid)
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# load_snapshot returns None when no game_state row exists
# ---------------------------------------------------------------------------


def test_load_snapshot_returns_none_when_absent(store: PgSnapshotStore) -> None:
    result = store.load_snapshot()
    assert result is None


# ---------------------------------------------------------------------------
# save_snapshot / load_snapshot round-trip
# ---------------------------------------------------------------------------


def test_save_load_snapshot_roundtrip(store: PgSnapshotStore) -> None:
    snap = GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        atmosphere="dark and stormy",
    )
    store.save_snapshot(snap)
    loaded = store.load_snapshot()

    assert loaded is not None
    assert loaded.snapshot.genre_slug == "test_genre"
    assert loaded.snapshot.world_slug == "test_world"
    assert loaded.snapshot.atmosphere == "dark and stormy"


def test_save_load_snapshot_meta_fields(store: PgSnapshotStore) -> None:
    """SavedSession.meta fields come from the sessions row."""
    snap = GameSnapshot(genre_slug="test_genre", world_slug="test_world")
    store.save_snapshot(snap)
    loaded = store.load_snapshot()

    assert loaded is not None
    assert loaded.meta.genre_slug == "test_genre"
    assert loaded.meta.world_slug == "test_world"
    assert isinstance(loaded.meta.created_at, datetime)
    assert isinstance(loaded.meta.last_played, datetime)


def test_save_snapshot_overwrites_previous(store: PgSnapshotStore) -> None:
    """Second save replaces first — upsert semantics."""
    snap1 = GameSnapshot(genre_slug="test_genre", world_slug="test_world", atmosphere="foggy")
    snap2 = GameSnapshot(genre_slug="test_genre", world_slug="test_world", atmosphere="sunny")
    store.save_snapshot(snap1)
    store.save_snapshot(snap2)
    loaded = store.load_snapshot()

    assert loaded is not None
    assert loaded.snapshot.atmosphere == "sunny"


def test_load_snapshot_with_narrative_produces_recap(store: PgSnapshotStore) -> None:
    """When narrative rows exist, load_snapshot assembles a non-None recap."""
    # Seed a narrative row directly via the pool so we can test the recap path
    # without A5's PgNarrativeStore (not built yet).
    pool = store._pool
    sid = store._session_id
    now = datetime.now(tz=UTC).isoformat()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO narrative_log (session_id, round_number, author, content, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (sid, 1, "narrator", "The party descended into the cavern.", now),
        )

    snap = GameSnapshot(genre_slug="test_genre", world_slug="test_world")
    store.save_snapshot(snap)
    loaded = store.load_snapshot()

    assert loaded is not None
    assert loaded.recap is not None
    assert "Previously" in loaded.recap


def test_load_snapshot_empty_narrative_recap_is_none(store: PgSnapshotStore) -> None:
    """No narrative rows + no known_facts → recap is None (mirrors SqliteStore)."""
    snap = GameSnapshot(genre_slug="test_genre", world_slug="test_world")
    store.save_snapshot(snap)
    loaded = store.load_snapshot()

    assert loaded is not None
    assert loaded.recap is None


# ---------------------------------------------------------------------------
# load_snapshot raises SaveSchemaIncompatibleError on corrupt JSON
# ---------------------------------------------------------------------------


def test_load_snapshot_raises_on_invalid_json(store: PgSnapshotStore) -> None:
    """Corrupt snapshot_json raises SaveSchemaIncompatibleError — not swallowed."""
    pool = store._pool
    sid = store._session_id
    now = datetime.now(tz=UTC).isoformat()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO game_state (session_id, snapshot_json, saved_at) VALUES (%s, %s, %s)",
            (sid, "NOT VALID JSON {{{", now),
        )
    with pytest.raises(SaveSchemaIncompatibleError):
        store.load_snapshot()


def test_load_snapshot_raises_on_schema_mismatch(store: PgSnapshotStore) -> None:
    """Pydantic-invalid snapshot raises SaveSchemaIncompatibleError."""
    pool = store._pool
    sid = store._session_id
    now = datetime.now(tz=UTC).isoformat()
    # Inject a narrative_log entry with a blank author — that will trigger
    # field validation if loaded through NarrativeEntry. Instead inject an
    # invalid GameSnapshot-level field by making characters a string (not list).
    bad = json.dumps({"characters": "not_a_list"})
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO game_state (session_id, snapshot_json, saved_at) VALUES (%s, %s, %s)",
            (sid, bad, now),
        )
    with pytest.raises(SaveSchemaIncompatibleError):
        store.load_snapshot()


# ---------------------------------------------------------------------------
# world_save round-trip
# ---------------------------------------------------------------------------


def test_load_world_save_returns_default_when_absent(store: PgSnapshotStore) -> None:
    """No world_save row → fresh WorldSave() (lazy default)."""
    result = store.load_world_save()
    assert isinstance(result, WorldSave)


def test_save_load_world_save_roundtrip(store: PgSnapshotStore) -> None:
    ws = WorldSave(delve_count=3)
    store.save_world_save(ws)
    loaded = store.load_world_save()

    assert loaded.delve_count == 3


def test_save_world_save_overwrites_previous(store: PgSnapshotStore) -> None:
    ws1 = WorldSave(delve_count=1)
    ws2 = WorldSave(delve_count=7)
    store.save_world_save(ws1)
    store.save_world_save(ws2)
    loaded = store.load_world_save()

    assert loaded.delve_count == 7


# ---------------------------------------------------------------------------
# Two independent sessions do not cross-contaminate
# ---------------------------------------------------------------------------


def test_two_sessions_are_isolated(monkeypatch, migrated_db: str) -> None:
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()

    slug_a = _unique_slug()
    slug_b = _unique_slug()
    sid_a = sessions.ensure_session(pool, slug=slug_a, mode="solo", genre_slug="g", world_slug="w")
    sid_b = sessions.ensure_session(pool, slug=slug_b, mode="solo", genre_slug="g", world_slug="w")

    store_a = PgSnapshotStore(pool, session_id=sid_a)
    store_b = PgSnapshotStore(pool, session_id=sid_b)

    snap_a = GameSnapshot(genre_slug="g", world_slug="w", atmosphere="session_a")
    store_a.save_snapshot(snap_a)

    # B has no snapshot yet; A's snapshot is invisible to B.
    assert store_b.load_snapshot() is None
    loaded_a = store_a.load_snapshot()
    assert loaded_a is not None
    assert loaded_a.snapshot.atmosphere == "session_a"

    db_pool.close_pool()
