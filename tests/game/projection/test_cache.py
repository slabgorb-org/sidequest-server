"""ProjectionCache — per-player decision cache backed by SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidequest.game.persistence import SqliteStore
from sidequest.game.projection.cache import CachedDecision, ProjectionCache
from sidequest.game.projection_filter import FilterDecision
from sidequest.game.sqlite_repository import SqliteSaveRepository


def _cache(tmp_path: Path) -> tuple[ProjectionCache, SqliteStore]:
    store = SqliteStore(tmp_path / "test.db")
    return ProjectionCache(SqliteSaveRepository(store)), store


def _insert_event(
    store: SqliteStore, seq: int, kind: str = "NARRATION", payload: str = "{}"
) -> None:
    """Insert a dummy event into the events table for foreign key constraint."""
    now = datetime.now(UTC).isoformat()
    with store._conn:
        store._conn.execute(
            "INSERT INTO events (seq, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (seq, kind, payload, now),
        )


def test_write_and_read_single_row(tmp_path: Path) -> None:
    cache, store = _cache(tmp_path)
    _insert_event(store, 1)
    dec = FilterDecision(include=True, payload_json='{"text":"hi"}')
    cache.write(event_seq=1, player_id="alice", decision=dec)
    rows = cache.read_since(player_id="alice", since_seq=0)
    assert rows == [CachedDecision(event_seq=1, include=True, payload_json='{"text":"hi"}')]


def test_read_since_filters_by_seq(tmp_path: Path) -> None:
    cache, store = _cache(tmp_path)
    _insert_event(store, 1)
    _insert_event(store, 2)
    _insert_event(store, 3)
    cache.write(event_seq=1, player_id="alice", decision=FilterDecision(True, '{"a":1}'))
    cache.write(event_seq=2, player_id="alice", decision=FilterDecision(True, '{"a":2}'))
    cache.write(event_seq=3, player_id="alice", decision=FilterDecision(True, '{"a":3}'))
    rows = cache.read_since(player_id="alice", since_seq=1)
    assert [r.event_seq for r in rows] == [2, 3]


def test_omitted_decision_stores_none_payload(tmp_path: Path) -> None:
    cache, store = _cache(tmp_path)
    _insert_event(store, 1)
    cache.write(event_seq=1, player_id="alice", decision=FilterDecision(False, ""))
    rows = cache.read_since(player_id="alice", since_seq=0)
    assert rows[0].include is False
    assert rows[0].payload_json is None


def test_multiple_players_isolated(tmp_path: Path) -> None:
    cache, store = _cache(tmp_path)
    _insert_event(store, 1)
    cache.write(event_seq=1, player_id="alice", decision=FilterDecision(True, '{"who":"alice"}'))
    cache.write(event_seq=1, player_id="bob", decision=FilterDecision(False, ""))
    assert cache.read_since(player_id="alice", since_seq=0)[0].payload_json == '{"who":"alice"}'
    assert cache.read_since(player_id="bob", since_seq=0)[0].include is False


def test_duplicate_write_is_idempotent_by_primary_key(tmp_path: Path) -> None:
    cache, store = _cache(tmp_path)
    _insert_event(store, 1)
    cache.write(event_seq=1, player_id="alice", decision=FilterDecision(True, '{"v":1}'))
    cache.write(event_seq=1, player_id="alice", decision=FilterDecision(True, '{"v":2}'))
    rows = cache.read_since(player_id="alice", since_seq=0)
    assert rows == [CachedDecision(event_seq=1, include=True, payload_json='{"v":2}')]
