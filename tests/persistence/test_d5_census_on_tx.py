"""D5 — census rides the turn transaction; explicit tx threading (ADR-115).

The concurrency linchpin. The deleted ``conn.in_transaction`` / ``MAX(seq)``
heuristic is replaced by EXPLICIT tx threading: ``publish_event`` decides
in-frame (``tx.write_telemetry`` — same connection as the open turn) vs
out-of-frame (``_telemetry_sink.record`` — own session_tx, event_seq=NULL)
purely by whether the ``tx`` parameter is set. No connection-state sniffing.

Tests here use a REAL ``PgSaveRepository`` + ``PgTelemetrySink`` over a
uuid-namespaced session on the ``migrated_db`` pool (NOT the SQLite
session_handler_factory). They assert:

  (a) IN-FRAME: open repo.transaction(); append_event; write_telemetry with
      that seq; after commit BOTH rows exist with event_seq == row.seq.
  (b) ROLLBACK: same in one tx, then raise; afterwards NEITHER row exists.
  (c) OUT-OF-FRAME: sink.record() writes event_seq IS NULL.
  (d) publish_event ROUTING: with tx= set, the row rides the turn tx
      (in-frame, event_seq attributed); with tx=None it goes through the
      bound sink (out-of-frame, event_seq NULL). Real Pg tx/sink, no mocks.
  (e) emit_mechanical_census FORWARDS tx + event_seq to publish_event, which
      lands the census + trope rows on the open turn tx.
  (f) bind_event_store WIRING (deferred from C1): a bound PgTelemetrySink +
      an out-of-frame publish_event persists a turn_telemetry row.
  (g) cross-session isolation (uuid slugs; pool COMMITS).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from sidequest.game import db_pool
from sidequest.game.mechanical_census import emit_mechanical_census
from sidequest.game.pg import sessions
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.telemetry import watcher_hub
from sidequest.telemetry.watcher_hub import bind_event_store, publish_event


def _slug() -> str:
    return f"sq_d5_{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@pytest.fixture
def repo_and_sink(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = _slug()
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    repo = PgSaveRepository(pool, session_id=sid)
    sink = PgTelemetrySink(pool, sid)
    # Always clear the process-global binding after the test so a bound sink
    # over a closed pool can't leak into another test (the heuristic is gone;
    # the only shared state is _telemetry_sink).
    try:
        yield repo, sink, pool, sid
    finally:
        bind_event_store(None)
        db_pool.close_pool()


# ---------------------------------------------------------------------------
# (a) IN-FRAME: write_telemetry rides the open transaction
# ---------------------------------------------------------------------------


def test_in_frame_event_and_telemetry_both_persist(repo_and_sink) -> None:
    repo, _sink, pool, sid = repo_and_sink

    with repo.transaction() as tx:
        row = tx.append_event(kind="NARRATION", payload_json="{}")
        tx.write_telemetry(
            event_seq=row.seq,
            round=1,
            ts=_now(),
            component="mechanical",
            event_type="census",
            payload_json="{}",
        )

    with pool.connection() as conn:
        erow = conn.execute(
            "SELECT seq FROM events WHERE session_id = %s AND seq = %s", (sid, row.seq)
        ).fetchone()
        trow = conn.execute(
            "SELECT event_seq FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
            (sid, row.seq),
        ).fetchone()
    assert erow is not None, "event row must persist on clean commit"
    assert trow is not None and trow[0] == row.seq, "telemetry row must ride the same tx"


# ---------------------------------------------------------------------------
# (b) ROLLBACK: shared transaction — both gone
# ---------------------------------------------------------------------------


def test_rollback_drops_both_event_and_telemetry(repo_and_sink) -> None:
    repo, _sink, pool, sid = repo_and_sink

    captured: list[int] = []
    with pytest.raises(RuntimeError, match="d5_rollback"), repo.transaction() as tx:
        row = tx.append_event(kind="DOOMED", payload_json="{}")
        tx.write_telemetry(
            event_seq=row.seq,
            round=2,
            ts=_now(),
            component="mechanical",
            event_type="census",
            payload_json="{}",
        )
        captured.append(row.seq)
        raise RuntimeError("d5_rollback")

    seq = captured[0]
    with pool.connection() as conn:
        erow = conn.execute(
            "SELECT seq FROM events WHERE session_id = %s AND seq = %s", (sid, seq)
        ).fetchone()
        trow = conn.execute(
            "SELECT event_seq FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
            (sid, seq),
        ).fetchone()
    assert erow is None, "rolled-back event must not persist"
    assert trow is None, "rolled-back telemetry must not persist (shared tx)"


# ---------------------------------------------------------------------------
# (c) OUT-OF-FRAME: sink.record() writes event_seq IS NULL
# ---------------------------------------------------------------------------


def test_out_of_frame_record_writes_null_event_seq(repo_and_sink) -> None:
    _repo, sink, pool, sid = repo_and_sink

    sink.record(
        round=1,
        ts=_now(),
        component="x",
        event_type="d5_oof",
        payload_json="{}",
    )

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT event_seq FROM turn_telemetry WHERE session_id = %s AND event_type = %s",
            (sid, "d5_oof"),
        ).fetchone()
    assert row is not None
    assert row[0] is None, "out-of-frame record must write event_seq=NULL"


# ---------------------------------------------------------------------------
# (d) publish_event ROUTING — explicit tx param decides in-frame vs sink
# ---------------------------------------------------------------------------


def test_publish_event_with_tx_rides_turn_tx(repo_and_sink) -> None:
    """publish_event(tx=, event_seq=) writes THROUGH tx (same connection),
    NOT through the bound sink — so no second connection is borrowed inside
    an open turn (the deadlock constraint)."""
    repo, sink, pool, sid = repo_and_sink
    # Bind a sink to prove that even when one is bound, the tx path does NOT
    # use it (no second pooled connection borrowed inside the turn tx).
    bind_event_store(sink)

    with repo.transaction() as tx:
        row = tx.append_event(kind="NARRATION", payload_json="{}")
        publish_event(
            "census",
            {"round": 7, "marker": "in_frame"},
            component="mechanical",
            tx=tx,
            event_seq=row.seq,
        )

    with pool.connection() as conn:
        trow = conn.execute(
            "SELECT event_seq, round FROM turn_telemetry WHERE session_id = %s AND event_type = %s",
            (sid, "census"),
        ).fetchone()
    assert trow is not None
    assert trow[0] == row.seq, "tx-threaded publish must attribute the turn's event_seq"
    assert trow[1] == 7


def test_publish_event_without_tx_uses_sink_out_of_frame(repo_and_sink) -> None:
    _repo, sink, pool, sid = repo_and_sink
    bind_event_store(sink)

    publish_event(
        "intent",
        {"round": 4, "label": "explore"},
        component="intent",
    )

    with pool.connection() as conn:
        trow = conn.execute(
            "SELECT event_seq, round FROM turn_telemetry WHERE session_id = %s AND event_type = %s",
            (sid, "intent"),
        ).fetchone()
    assert trow is not None
    assert trow[0] is None, "no tx → out-of-frame sink.record → event_seq NULL"
    assert trow[1] == 4


def test_publish_event_without_tx_no_sink_is_noop(repo_and_sink) -> None:
    """No bound sink (legacy/in-memory) → out-of-frame publish is a no-op,
    not an error (mirrors the historical ``store is None`` guard)."""
    _repo, _sink, _pool, _sid = repo_and_sink
    bind_event_store(None)
    # Must not raise.
    publish_event("intent", {"round": 1}, component="intent")


# ---------------------------------------------------------------------------
# (e) emit_mechanical_census forwards tx + event_seq to publish_event
# ---------------------------------------------------------------------------


class _FakeRoom:
    def __init__(self, pids):
        self._pids = pids

    def playing_player_ids(self):
        return list(self._pids)


class _Core:
    def __init__(self, name):
        self.name = name
        self.hp = type("Hp", (), {"current": 5, "max": 10, "base_max": 10})()
        self.statuses = []
        self.inventory = type("Inv", (), {"items": [], "gold": 0})()
        self.xp = 0
        self.level = 1
        self.acquired_advancements = []


class _Char:
    def __init__(self, name):
        self.core = _Core(name)
        self.current_room = "r1"
        self.abilities = []

    def is_broken(self):
        return False


class _Snapshot:
    def __init__(self):
        self.player_seats = {"p1": "Alice"}
        self.characters = [_Char("Alice")]
        self.character_locations = {"Alice": "tavern"}
        self.turn_manager = type("TM", (), {"interaction": 3})()
        self.active_tropes = []
        self.turns_since_meaningful = 0
        self.total_beats_fired = 0


def test_emit_mechanical_census_lands_on_turn_tx(repo_and_sink) -> None:
    """emit_mechanical_census(tx=, event_seq=) threads both through to
    publish_event so the census + trope rows ride the open turn tx with the
    event's seq."""
    repo, _sink, pool, sid = repo_and_sink
    room = _FakeRoom(["p1"])
    snap = _Snapshot()

    with repo.transaction() as tx:
        row = tx.append_event(kind="NARRATION", payload_json="{}")
        emit_mechanical_census(room, snap, tx=tx, event_seq=row.seq)

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT event_type, event_seq FROM turn_telemetry "
            "WHERE session_id = %s ORDER BY event_type",
            (sid,),
        ).fetchall()
    by_type = {r[0]: r[1] for r in rows}
    assert by_type.get("census") == row.seq, "per-PC census must ride the turn tx"
    assert by_type.get("trope_census") == row.seq, "trope census must ride the turn tx"


# ---------------------------------------------------------------------------
# (f) bind_event_store wiring (deferred from C1)
# ---------------------------------------------------------------------------


def test_bind_event_store_wires_the_sink(repo_and_sink) -> None:
    """A bound PgTelemetrySink + an out-of-frame publish_event persists a
    turn_telemetry row through the bound sink — proving the binding is wired."""
    _repo, sink, pool, sid = repo_and_sink
    bind_event_store(sink)
    assert watcher_hub._telemetry_sink is sink

    publish_event("subsystem_trace", {"round": 9}, component="probe")

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT event_seq, round, component FROM turn_telemetry "
            "WHERE session_id = %s AND event_type = %s",
            (sid, "subsystem_trace"),
        ).fetchone()
    assert row is not None, "bound sink must have persisted the out-of-frame row"
    assert row[0] is None
    assert row[1] == 9
    assert row[2] == "probe"


# ---------------------------------------------------------------------------
# (g) cross-session isolation
# ---------------------------------------------------------------------------


def test_cross_session_isolation(repo_and_sink) -> None:
    _repo_a, sink_a, pool, sid_a = repo_and_sink
    sid_b = sessions.ensure_session(pool, slug=_slug(), mode="solo", genre_slug="g", world_slug="w")

    sink_a.record(round=1, ts=_now(), component="c", event_type="iso", payload_json="{}")

    with pool.connection() as conn:
        count_b = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry WHERE session_id = %s", (sid_b,)
        ).fetchone()[0]
        count_a = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry WHERE session_id = %s", (sid_a,)
        ).fetchone()[0]
    assert count_b == 0, "session B sees no telemetry from A"
    assert count_a == 1
