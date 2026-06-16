"""SQLite-backed SaveRepository (ADR-115, Phase 0).

Wraps the existing SqliteStore. Preserves the SAVE_WRITE_LOCK acquisition
order (lock OUTSIDE the connection transaction) so concurrent writers on
the shared check_same_thread=False connection do not corrupt per-statement
state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sidequest.game.event_log import EventRow
from sidequest.game.persistence import SAVE_WRITE_LOCK, SqliteStore
from sidequest.game.projection.cache import CachedDecision
from sidequest.game.projection_filter import FilterDecision
from sidequest.telemetry.spans import projection_cache_fill_span


class _SqliteSaveTransaction:
    """Operations bound to one open SQLite connection transaction. Does not
    commit — the owning ``SqliteSaveRepository.transaction()`` block commits
    on clean exit, rolls back on exception."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append_event(self, *, kind: str, payload_json: str) -> EventRow:
        now = datetime.now(tz=UTC).isoformat()
        cur = self._conn.execute(
            "INSERT INTO events (kind, payload_json, created_at) VALUES (?, ?, ?)",
            (kind, payload_json, now),
        )
        seq = cur.lastrowid
        assert seq is not None
        return EventRow(seq=seq, kind=kind, payload_json=payload_json, created_at=now)

    def write_projection(self, *, event_seq: int, player_id: str, decision: FilterDecision) -> None:
        with projection_cache_fill_span(event_seq=event_seq, player_id=player_id):
            payload = decision.payload_json if decision.include else None
            self._conn.execute(
                """
                INSERT INTO projection_cache (event_seq, player_id, include, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_seq, player_id) DO UPDATE SET
                    include = excluded.include,
                    payload_json = excluded.payload_json
                """,
                (event_seq, player_id, 1 if decision.include else 0, payload),
            )


class SqliteSaveRepository:
    """SaveRepository over a SqliteStore."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def store(self) -> SqliteStore:
        """TRANSITIONAL escape hatch (ADR-115 P0). A later slice migrates the
        remaining raw-connection consumers (scrapbook persist, views reads)
        and removes this. Do not add new callers."""
        return self._store

    @contextmanager
    def transaction(self) -> Iterator[_SqliteSaveTransaction]:
        # Lock first, then the connection transaction — mandatory order.
        with SAVE_WRITE_LOCK, self._store._conn:
            yield _SqliteSaveTransaction(self._store._conn)

    def append_event(self, *, kind: str, payload_json: str) -> EventRow:
        with self.transaction() as tx:
            return tx.append_event(kind=kind, payload_json=payload_json)

    def read_events_since(self, *, since_seq: int) -> list[EventRow]:
        with self._store._conn:
            rows = self._store._conn.execute(
                "SELECT seq, kind, payload_json, created_at FROM events "
                "WHERE seq > ? ORDER BY seq ASC",
                (since_seq,),
            ).fetchall()
        return [EventRow(seq=r[0], kind=r[1], payload_json=r[2], created_at=r[3]) for r in rows]

    def latest_event_seq(self) -> int:
        with self._store._conn:
            row = self._store._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return int(row[0])

    def write_projection(self, *, event_seq: int, player_id: str, decision: FilterDecision) -> None:
        with self.transaction() as tx:
            tx.write_projection(event_seq=event_seq, player_id=player_id, decision=decision)

    def read_projection_since(self, *, player_id: str, since_seq: int) -> list[CachedDecision]:
        with self._store._conn:
            rows = self._store._conn.execute(
                """
                SELECT event_seq, include, payload_json
                FROM projection_cache
                WHERE player_id = ? AND event_seq > ?
                ORDER BY event_seq ASC
                """,
                (player_id, since_seq),
            ).fetchall()
        return [CachedDecision(event_seq=r[0], include=bool(r[1]), payload_json=r[2]) for r in rows]
