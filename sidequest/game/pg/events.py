"""Events (per-session seq) + projection_cache over Postgres (ADR-115).

PgSaveTransaction is the unit-of-work bound to one locked connection; the
census/telemetry write rides the same transaction via write_telemetry.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from psycopg_pool import ConnectionPool

from sidequest.game.event_log import EventRow
from sidequest.game.pg._conn import session_tx
from sidequest.game.projection.cache import CachedDecision
from sidequest.game.projection_filter import FilterDecision
from sidequest.telemetry.spans import projection_cache_fill_span

_INSERT_EVENT = """
INSERT INTO events (session_id, seq, kind, payload_json, created_at)
VALUES (%s, (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE session_id = %s), %s, %s, %s)
RETURNING seq
"""

_UPSERT_PROJECTION = """
INSERT INTO projection_cache (session_id, event_seq, player_id, include, payload_json)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (session_id, event_seq, player_id) DO UPDATE SET
    include = excluded.include, payload_json = excluded.payload_json
"""

_INSERT_TELEMETRY = """
INSERT INTO turn_telemetry (session_id, event_seq, round, ts, component, event_type, payload_json)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


class PgSaveTransaction:
    """Operations bound to one locked connection; no individual commit."""

    def __init__(self, conn: psycopg.Connection, session_id: int) -> None:
        self._conn = conn
        self._sid = session_id

    def append_event(self, *, kind: str, payload_json: str) -> EventRow:
        now = datetime.now(tz=UTC).isoformat()
        seq = self._conn.execute(
            _INSERT_EVENT, (self._sid, self._sid, kind, payload_json, now)
        ).fetchone()[0]
        return EventRow(seq=int(seq), kind=kind, payload_json=payload_json, created_at=now)

    def write_projection(self, *, event_seq: int, player_id: str, decision: FilterDecision) -> None:
        with projection_cache_fill_span(event_seq=event_seq, player_id=player_id):
            payload = decision.payload_json if decision.include else None
            self._conn.execute(
                _UPSERT_PROJECTION,
                (self._sid, event_seq, player_id, 1 if decision.include else 0, payload),
            )

    def write_telemetry(
        self,
        *,
        event_seq: int | None,
        round: int | None,
        ts: str,
        component: str,
        event_type: str,
        payload_json: str,
    ) -> None:
        """Census/telemetry write riding the turn's transaction (event_seq is
        the real seq from append_event in-frame, or None out-of-frame)."""
        self._conn.execute(
            _INSERT_TELEMETRY,
            (self._sid, event_seq, round, ts, component, event_type, payload_json),
        )


class PgEventStore:
    """Convenience facade over single-statement event/projection ops (each in
    its own locked transaction). The owning PgSaveRepository delegates here."""

    def __init__(self, pool: ConnectionPool, *, session_id: int) -> None:
        self._pool = pool
        self._sid = session_id

    def append_event(self, *, kind: str, payload_json: str) -> EventRow:
        with session_tx(self._pool, self._sid) as conn:
            return PgSaveTransaction(conn, self._sid).append_event(
                kind=kind, payload_json=payload_json
            )

    def write_projection(self, *, event_seq: int, player_id: str, decision: FilterDecision) -> None:
        with session_tx(self._pool, self._sid) as conn:
            PgSaveTransaction(conn, self._sid).write_projection(
                event_seq=event_seq, player_id=player_id, decision=decision
            )

    def read_events_since(self, *, since_seq: int) -> list[EventRow]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT seq, kind, payload_json, created_at FROM events "
                "WHERE session_id = %s AND seq > %s ORDER BY seq ASC",
                (self._sid, since_seq),
            ).fetchall()
        return [EventRow(seq=r[0], kind=r[1], payload_json=r[2], created_at=r[3]) for r in rows]

    def latest_event_seq(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = %s", (self._sid,)
            ).fetchone()
        return int(row[0])

    def read_projection_since(self, *, player_id: str, since_seq: int) -> list[CachedDecision]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT event_seq, include, payload_json FROM projection_cache "
                "WHERE session_id = %s AND player_id = %s AND event_seq > %s ORDER BY event_seq ASC",
                (self._sid, player_id, since_seq),
            ).fetchall()
        return [CachedDecision(event_seq=r[0], include=bool(r[1]), payload_json=r[2]) for r in rows]
