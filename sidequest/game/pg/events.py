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
from sidequest.telemetry.spans import (
    projection_cache_fill_span,
    projection_cache_prune_span,
    turn_telemetry_prune_span,
)

# Story 126-22 — save-DB retention bounds (applied on the production save path).
# projection_cache is a lazy-refillable cache, so it keeps only the newest N
# event_seqs per (session, player); turn_telemetry is forensic, so it keeps the
# newest N distinct rounds (a generous tail). Both are the cause-fix for the
# measured 4.3M-row projection_cache / 470k-row turn_telemetry bloat.
PROJECTION_CACHE_KEEP_LAST_PER_PLAYER = 200
TURN_TELEMETRY_KEEP_LAST_ROUNDS = 100

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

# Keep only the newest ``keep_last_per_player`` event_seqs per player for this
# session; delete the rest. The window read uses the PK
# (session_id, event_seq, player_id). Parameterized — session_id never
# interpolated (CWE-89).
_PRUNE_PROJECTION = """
DELETE FROM projection_cache
WHERE session_id = %s
  AND (player_id, event_seq) IN (
    SELECT player_id, event_seq FROM (
      SELECT player_id, event_seq,
             ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY event_seq DESC) AS rn
      FROM projection_cache
      WHERE session_id = %s
    ) ranked
    WHERE ranked.rn > %s
  )
"""

# Keep the newest ``keep_last_rounds`` DISTINCT rounds for this session; delete
# older rows. Rows with NULL round (out-of-frame writes) are un-attributable and
# are retained (No Silent Fallbacks) via the ``round IS NOT NULL`` guard.
_PRUNE_TELEMETRY = """
DELETE FROM turn_telemetry
WHERE session_id = %s
  AND round IS NOT NULL
  AND round < (
    SELECT MIN(kept.r) FROM (
      SELECT DISTINCT round AS r FROM turn_telemetry
      WHERE session_id = %s AND round IS NOT NULL
      ORDER BY r DESC LIMIT %s
    ) kept
  )
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

    def prune_projection_cache(self, *, keep_last_per_player: int) -> int:
        """Bound projection_cache to the newest ``keep_last_per_player`` event_seqs
        per (session, player); return the number of rows deleted.

        Safe because projection_cache is a pure cache — ``projection/cache_fill``
        rebuilds any pruned rows from the event log on the player's next connect,
        and live resume only reads the head of the window. Emits a
        ``projection.cache.prune`` span (rows_pruned + session_id) for the GM panel.
        """
        if keep_last_per_player < 1:
            # Fail loud: 0 deletes the whole session cache (rn > 0 matches every
            # row); negatives are nonsensical. The production constant is 200.
            raise ValueError(f"keep_last_per_player must be >= 1 (got {keep_last_per_player})")
        with projection_cache_prune_span(session_id=self._sid) as span:
            pruned = 0
            try:
                with session_tx(self._pool, self._sid) as conn:
                    cur = conn.execute(
                        _PRUNE_PROJECTION, (self._sid, self._sid, keep_last_per_player)
                    )
                    pruned = cur.rowcount
            finally:
                # Emit rows_pruned even if the tx raised, so the GM panel can tell
                # "pruned 0" from "prune errored" (the span also carries error status).
                span.set_attribute("rows_pruned", pruned)
        return pruned

    def prune_turn_telemetry(self, *, keep_last_rounds: int) -> int:
        """Retain only the newest ``keep_last_rounds`` distinct rounds for this
        session; return the number of rows deleted.

        NULL-round (out-of-frame) rows are un-attributable and are kept — a
        round-based retention must not silently drop them (No Silent Fallbacks).
        Emits a ``turn_telemetry.prune`` span (rows_pruned + session_id).
        """
        if keep_last_rounds < 1:
            # Fail loud: 0 is a silent no-op here (LIMIT 0 -> empty -> MIN NULL ->
            # deletes nothing) — the inverse of prune_projection_cache's 0. Reject
            # both. The production constant is 100.
            raise ValueError(f"keep_last_rounds must be >= 1 (got {keep_last_rounds})")
        with turn_telemetry_prune_span(session_id=self._sid) as span:
            pruned = 0
            try:
                with session_tx(self._pool, self._sid) as conn:
                    cur = conn.execute(_PRUNE_TELEMETRY, (self._sid, self._sid, keep_last_rounds))
                    pruned = cur.rowcount
            finally:
                span.set_attribute("rows_pruned", pruned)
        return pruned
