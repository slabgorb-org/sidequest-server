"""PgTelemetrySink — Postgres telemetry sink (ADR-115 C1).

ONE SINK, TWO ENTRY POINTS — not a parallel mechanism
------------------------------------------------------
The out-of-frame SQLite watcher hub path (_persist_turn_telemetry) had two
branches that this module ports:

  1. ``in_transaction`` branch  → NOT here.  The in-frame write is
     ``PgSaveTransaction.write_telemetry`` (Task A3, events.py), called by
     ``emit_mechanical_census`` inside the open ``emit_event`` transaction
     in Task-Group D.  That path rides the same locked connection as the
     event INSERT so the telemetry row and the event row commit atomically.

  2. ``else: with conn:`` branch → ``PgTelemetrySink.record()``.
     Called when a watcher event fires OUTSIDE an event frame (e.g. a
     standalone subsystem trace).  Opens its own short ``session_tx``; writes
     ``event_seq=NULL`` to signal "out-of-frame".

And one encounter-row path that ports ``_maybe_persist_encounter_row``'s
``INSERT INTO events`` + commit:

  3. ``PgTelemetrySink.append_encounter_event()`` → delegates to
     ``PgSaveTransaction.append_event`` inside a ``session_tx`` so the
     per-session MAX(seq)+1 mechanism is reused without a second seq-
     assignment path (one mechanism per problem).

This is NOT a parallel mechanism.  The heuristic in watcher_hub.py
(``conn.in_transaction`` ⟺ "we're in a turn") is DELETED in Task D5 when
the sink is wired into the emitter.  C1 only builds the object; D5 does the
wiring and deletes the heuristic.

No OTEL spans are emitted here: the originals (_persist_turn_telemetry and
_maybe_persist_encounter_row) emit none; none are added here.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from sidequest.game.event_log import EventRow
from sidequest.game.pg._conn import session_tx
from sidequest.game.pg.events import _INSERT_TELEMETRY, PgSaveTransaction


class OutOfFrameSessionMissing(Exception):
    """Raised by :meth:`PgTelemetrySink.record` when the bound ``session_id``
    has no ``sessions`` row.

    Signals a stale sink (the room was torn down but the sink/ContextVar
    binding outlived it) or a bind-before-commit race — NOT a sink bug. Lets
    the out-of-frame telemetry path drop the write with one clean log line
    instead of an opaque per-event ``ForeignKeyViolation`` traceback
    (playtest 2026-06-11)."""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        super().__init__(
            f"out-of-frame telemetry write against session_id={session_id} "
            "with no sessions row (stale sink or bind-before-commit race)"
        )


class PgTelemetrySink:
    """Postgres out-of-frame telemetry + encounter-event sink.

    Instantiated with a pool and the integer session_id for the live session.
    Call ``record()`` from any watcher publish path that fires outside an open
    event transaction; call ``append_encounter_event()`` to replace the
    ``_maybe_persist_encounter_row`` raw INSERT.

    The in-frame path lives on ``PgSaveTransaction.write_telemetry`` — not here.
    """

    def __init__(self, pool: ConnectionPool, session_id: int) -> None:
        self._pool = pool
        self._sid = session_id

    def record(
        self,
        *,
        round: int | None,
        ts: str,
        component: str,
        event_type: str,
        payload_json: str,
    ) -> None:
        """Out-of-frame telemetry write: event_seq is always NULL.

        Mirrors the ``else: with conn:`` branch of
        ``watcher_hub._persist_turn_telemetry`` exactly:
          - event_seq  = NULL  (no open event frame)
          - round      = caller-supplied int or None
          - ts         = caller-supplied ISO-8601 TEXT (watcher_hub passes
                         ``datetime.now(UTC).isoformat()`` at publish time)
          - component  = event["component"]
          - event_type = event["event_type"]
          - payload_json = json.dumps(event["fields"])

        Opens its own short ``session_tx`` (takes the per-session row lock,
        commits on exit, rolls back on exception).  Never raises — failures
        must be caught by the caller (D5 will wrap the call the same way
        watcher_hub._persist_turn_telemetry does today).
        """
        with session_tx(self._pool, self._sid) as conn:
            # No Silent Fallbacks: session_tx's SELECT ... FOR UPDATE locks
            # nothing when the row is absent, so a bare INSERT would throw an
            # opaque ForeignKeyViolation. Detect the missing row inside the same
            # tx and raise a typed, catchable error instead — the out-of-frame
            # caller drops the write with one clean line, no per-event traceback.
            exists = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = %s", (self._sid,)
            ).fetchone()
            if exists is None:
                raise OutOfFrameSessionMissing(self._sid)
            conn.execute(
                _INSERT_TELEMETRY,
                (self._sid, None, round, ts, component, event_type, payload_json),
            )

    def append_encounter_event(self, *, kind: str, payload_json: str) -> EventRow:
        """Append an encounter event row to the events table; return its EventRow.

        Replaces ``watcher_hub._maybe_persist_encounter_row``'s raw
        ``INSERT INTO events (kind, payload_json, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)``
        + commit.

        Reuses ``PgSaveTransaction.append_event`` so the per-session
        MAX(seq)+1 seq-assignment lives in exactly ONE place (one mechanism
        per problem).  ``created_at`` moves from DB-side ``CURRENT_TIMESTAMP``
        to Python-side ``datetime.now(UTC).isoformat()`` — documented
        intentional change (ISO-8601 consistency across the pg/ package).
        """
        with session_tx(self._pool, self._sid) as conn:
            tx = PgSaveTransaction(conn, self._sid)
            return tx.append_event(kind=kind, payload_json=payload_json)
