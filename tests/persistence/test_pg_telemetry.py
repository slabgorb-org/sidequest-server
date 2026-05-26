"""PgTelemetrySink — out-of-frame + encounter-event writes (ADR-115 C1).

Three test cases:

(a) In-frame: existing PgSaveRepository.transaction() + write_telemetry + append_event
    share one transaction; rollback removes both the event and telemetry row.

(b) Out-of-frame: PgTelemetrySink.record() writes a row with event_seq IS NULL in
    its own short session_tx.

(c) Encounter-row write: PgTelemetrySink.append_encounter_event() appends to events
    and returns an EventRow with a real seq.

Also: TelemetrySink Protocol isinstance check; cross-session isolation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.game.repository import TelemetrySink


def _slug() -> str:
    return f"sq_c1_{uuid.uuid4().hex[:8]}"


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
    yield repo, sink, pool, sid
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_pg_telemetry_sink_satisfies_protocol(repo_and_sink) -> None:
    """PgTelemetrySink must pass isinstance(sink, TelemetrySink)."""
    _repo, sink, _pool, _sid = repo_and_sink
    assert isinstance(sink, TelemetrySink)


# ---------------------------------------------------------------------------
# (a) In-frame: write_telemetry rides the open transaction (A3)
# ---------------------------------------------------------------------------


def test_in_frame_telemetry_commits_with_event(repo_and_sink) -> None:
    """Event + telemetry written in one transaction both persist on clean exit."""
    repo, _sink, pool, sid = repo_and_sink

    with repo.transaction() as tx:
        ev = tx.append_event(kind="NARRATION", payload_json="{}")
        tx.write_telemetry(
            event_seq=ev.seq,
            round=1,
            ts=_now(),
            component="mechanical",
            event_type="test_in_frame",
            payload_json="{}",
        )

    # Both rows durable after commit.
    with pool.connection() as conn:
        trow = conn.execute(
            "SELECT event_seq, round FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
            (sid, ev.seq),
        ).fetchone()
    assert trow is not None
    assert trow[0] == ev.seq
    assert trow[1] == 1

    with pool.connection() as conn:
        erow = conn.execute(
            "SELECT seq FROM events WHERE session_id = %s AND seq = %s",
            (sid, ev.seq),
        ).fetchone()
    assert erow is not None


def test_in_frame_telemetry_rolls_back_with_event(repo_and_sink) -> None:
    """If the transaction raises, BOTH the event and telemetry row are gone.

    This proves shared-tx: the rollback is atomic across events + turn_telemetry.
    """
    repo, _sink, pool, sid = repo_and_sink

    captured_seq: list[int] = []
    with pytest.raises(RuntimeError, match="shared_tx_rollback"), repo.transaction() as tx:
        ev = tx.append_event(kind="DOOMED", payload_json="{}")
        tx.write_telemetry(
            event_seq=ev.seq,
            round=2,
            ts=_now(),
            component="mechanical",
            event_type="test_rollback",
            payload_json="{}",
        )
        captured_seq.append(ev.seq)
        raise RuntimeError("shared_tx_rollback")

    assert captured_seq, "must have captured the seq before the raise"
    seq = captured_seq[0]

    with pool.connection() as conn:
        erow = conn.execute(
            "SELECT seq FROM events WHERE session_id = %s AND seq = %s", (sid, seq)
        ).fetchone()
        trow = conn.execute(
            "SELECT event_seq FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
            (sid, seq),
        ).fetchone()
    assert erow is None, "rolled-back event must not persist"
    assert trow is None, "rolled-back telemetry row must not persist"


# ---------------------------------------------------------------------------
# (b) Out-of-frame: PgTelemetrySink.record() — event_seq IS NULL
# ---------------------------------------------------------------------------


def test_record_writes_row_with_null_event_seq(repo_and_sink) -> None:
    """PgTelemetrySink.record() opens its own short session_tx; event_seq IS NULL."""
    _repo, sink, pool, sid = repo_and_sink

    ts = _now()
    sink.record(
        round=3,
        ts=ts,
        component="test_comp",
        event_type="out_of_frame_event",
        payload_json='{"x":1}',
    )

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT event_seq, round, component, event_type, payload_json "
            "FROM turn_telemetry "
            "WHERE session_id = %s AND event_type = %s",
            (sid, "out_of_frame_event"),
        ).fetchone()
    assert row is not None
    assert row[0] is None, "event_seq must be NULL for out-of-frame record"
    assert row[1] == 3
    assert row[2] == "test_comp"
    assert row[3] == "out_of_frame_event"
    assert row[4] == '{"x":1}'


def test_record_null_round_accepted(repo_and_sink) -> None:
    """record() with round=None (no round context) writes event_seq=NULL, round=NULL."""
    _repo, sink, pool, sid = repo_and_sink

    sink.record(
        round=None,
        ts=_now(),
        component="c",
        event_type="no_round",
        payload_json="{}",
    )

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT event_seq, round FROM turn_telemetry WHERE session_id = %s AND event_type = %s",
            (sid, "no_round"),
        ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None


# ---------------------------------------------------------------------------
# (c) append_encounter_event — delegates to PgSaveTransaction.append_event
# ---------------------------------------------------------------------------


def test_append_encounter_event_returns_event_row_with_seq(repo_and_sink) -> None:
    """append_encounter_event appends to events and returns a real EventRow.seq."""
    _repo, sink, pool, sid = repo_and_sink

    row = sink.append_encounter_event(kind="ENCOUNTER_START", payload_json='{"op":"started"}')

    assert row.seq >= 1
    assert row.kind == "ENCOUNTER_START"
    # Guards the documented CURRENT_TIMESTAMP → ISO-now change: created_at is a
    # non-empty ISO-8601 string parseable by datetime.fromisoformat.
    assert isinstance(row.created_at, str) and row.created_at
    datetime.fromisoformat(row.created_at)

    with pool.connection() as conn:
        db_row = conn.execute(
            "SELECT seq, kind FROM events WHERE session_id = %s AND seq = %s",
            (sid, row.seq),
        ).fetchone()
    assert db_row is not None
    assert db_row[1] == "ENCOUNTER_START"


def test_append_encounter_event_seq_is_monotonic(repo_and_sink) -> None:
    """Multiple append_encounter_event calls produce strictly increasing seqs."""
    _repo, sink, _pool, _sid = repo_and_sink

    a = sink.append_encounter_event(kind="ENCOUNTER_START", payload_json="{}")
    b = sink.append_encounter_event(kind="ENCOUNTER_BEAT_APPLIED", payload_json="{}")
    assert b.seq == a.seq + 1


# ---------------------------------------------------------------------------
# Cross-session isolation
# ---------------------------------------------------------------------------


def test_cross_session_isolation(repo_and_sink) -> None:
    """Sink A's writes are invisible to sink B (different session_id, same pool)."""
    _repo_a, sink_a, pool, sid_a = repo_and_sink

    # Build a second session on the same pool.
    slug_b = _slug()
    sid_b = sessions.ensure_session(pool, slug=slug_b, mode="solo", genre_slug="g", world_slug="w")

    # Write through sink A.
    sink_a.record(round=1, ts=_now(), component="c", event_type="iso_evt", payload_json="{}")
    ev_a = sink_a.append_encounter_event(kind="ENCOUNTER_START", payload_json="{}")

    # Sink B sees no telemetry or events from A.
    with pool.connection() as conn:
        t_count_b = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry WHERE session_id = %s", (sid_b,)
        ).fetchone()[0]
        e_count_b = conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = %s", (sid_b,)
        ).fetchone()[0]
    assert t_count_b == 0, "session B must see no telemetry rows from A"
    assert e_count_b == 0, "session B must see no event rows from A"

    # Sink A sees its own writes.
    with pool.connection() as conn:
        t_count_a = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry WHERE session_id = %s", (sid_a,)
        ).fetchone()[0]
        e_count_a = conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = %s AND seq = %s",
            (sid_a, ev_a.seq),
        ).fetchone()[0]
    assert t_count_a == 1
    assert e_count_a == 1
