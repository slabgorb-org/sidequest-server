"""Events seq assignment + projection upsert over Postgres (ADR-115)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg._conn import session_tx
from sidequest.game.pg.events import PgEventStore, PgSaveTransaction
from sidequest.game.projection_filter import FilterDecision


@pytest.fixture
def store(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"g_w_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgEventStore(pool, session_id=sid)
    db_pool.close_pool()


def test_append_event_assigns_monotonic_per_session_seq(store) -> None:
    a = store.append_event(kind="NARRATION", payload_json="{}")
    b = store.append_event(kind="NARRATION", payload_json="{}")
    assert (a.seq, b.seq) == (1, 2)


def test_read_events_since(store) -> None:
    store.append_event(kind="A", payload_json="{}")
    store.append_event(kind="B", payload_json="{}")
    rows = store.read_events_since(since_seq=1)
    assert [r.kind for r in rows] == ["B"]


def test_latest_event_seq_zero_when_empty(store) -> None:
    assert store.latest_event_seq() == 0


def test_projection_upsert_then_read(store) -> None:
    ev = store.append_event(kind="NARRATION", payload_json="{}")
    store.write_projection(
        event_seq=ev.seq,
        player_id="p1",
        decision=FilterDecision(include=True, payload_json='{"x":1}'),
    )
    store.write_projection(
        event_seq=ev.seq, player_id="p1", decision=FilterDecision(include=False, payload_json="{}")
    )
    rows = store.read_projection_since(player_id="p1", since_seq=0)
    assert len(rows) == 1 and rows[0].include is False


def test_write_telemetry_in_frame_and_out_of_frame(store) -> None:
    """write_telemetry rides the turn's locked transaction (census primitive).

    In-frame: event_seq is the real seq from append_event in the same tx.
    Out-of-frame: event_seq/round are NULL — guards the nullable columns.
    """
    pool = store._pool
    sid = store._sid

    # IN-FRAME: append + telemetry in the same locked transaction.
    with session_tx(pool, sid) as conn:
        tx = PgSaveTransaction(conn, sid)
        ev = tx.append_event(kind="NARRATION", payload_json="{}")
        tx.write_telemetry(
            event_seq=ev.seq,
            round=1,
            ts=datetime.now(tz=UTC).isoformat(),
            component="mechanical",
            event_type="x",
            payload_json="{}",
        )
        in_frame_seq = ev.seq

    # Fresh connection after the tx committed — the row is durable.
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT event_seq, round FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
            (sid, in_frame_seq),
        ).fetchone()
    assert row is not None and row[0] == in_frame_seq and row[1] == 1

    # OUT-OF-FRAME: NULL event_seq + NULL round must not raise.
    with session_tx(pool, sid) as conn:
        tx = PgSaveTransaction(conn, sid)
        tx.write_telemetry(
            event_seq=None,
            round=None,
            ts=datetime.now(tz=UTC).isoformat(),
            component="c",
            event_type="y",
            payload_json="{}",
        )

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT event_seq, round FROM turn_telemetry WHERE session_id = %s AND event_type = %s",
            (sid, "y"),
        ).fetchone()
    assert row is not None and row[0] is None and row[1] is None
