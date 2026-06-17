"""Characterization: pins the invariant the mechanical census rests on.

R1 / ADR-115 D5: the C2 turn write txn is the open ``SaveTransaction`` in
emitters.emit_event's ``with repo.transaction() as tx:`` block. A publish_event
issued inside that block with ``tx=tx, event_seq=seq`` rides the C2 txn on the
SAME connection: the row is attributed ``event_seq`` and rolls back atomically
with the turn. The in-frame vs out-of-frame split is the EXPLICIT ``tx``
parameter — never connection-state sniffing. If the in-frame attribution
fails, the census cannot be made atomic with the turn — STOP and escalate.
"""

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.telemetry.watcher_hub import bind_event_store, publish_event


@pytest.fixture
def repo_and_sink(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"sq_census_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    repo = PgSaveRepository(pool, session_id=sid)
    sink = PgTelemetrySink(pool, sid)
    yield repo, sink, pool, sid
    db_pool.close_pool()


def test_publish_inside_emit_style_block_rides_the_c2_txn(repo_and_sink):
    """Simulates emit_event's ``with repo.transaction() as tx:`` block: the
    events row is appended first (tx.append_event), THEN a
    component='mechanical' publish is threaded through ``tx``/``event_seq``.
    The census row must (a) attribute event_seq = the in-flight events row and
    (b) roll back with the turn."""
    repo, sink, pool, sid = repo_and_sink
    try:
        bind_event_store(sink)
        captured_seq: list[int] = []
        with pytest.raises(RuntimeError, match="force rollback"), repo.transaction() as tx:
            ev = tx.append_event(kind="NARRATION", payload_json="{}")
            publish_event(
                "census",
                {"player_id": "p1", "round": 4},
                component="mechanical",
                tx=tx,
                event_seq=ev.seq,
            )
            captured_seq.append(ev.seq)
            inflight = [
                tuple(r)
                for r in tx._conn.execute(  # noqa: SLF001 — read the in-flight tx
                    "SELECT event_seq, round, component, event_type FROM turn_telemetry "
                    "WHERE session_id = %s",
                    (sid,),
                ).fetchall()
            ]
            # rides the txn: event_seq = the in-flight event's seq
            assert inflight == [(ev.seq, 4, "mechanical", "census")]
            raise RuntimeError("force rollback")

        seq = captured_seq[0]
        # turn rolled back -> census rolled back atomically with it
        with pool.connection() as conn:
            t_count = conn.execute(
                "SELECT COUNT(*) FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
                (sid, seq),
            ).fetchone()[0]
            e_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = %s AND seq = %s",
                (sid, seq),
            ).fetchone()[0]
        assert t_count == 0
        assert e_count == 0
    finally:
        bind_event_store(None)


def test_publish_outside_any_block_does_not_ride_a_turn(repo_and_sink):
    """Sanity foil: a mechanical publish with no ``tx`` threaded takes the
    sink's own short txn and event_seq is NULL (NOT attributed to a turn).
    Proves the in-txn attribution in the test above is the meaningful
    signal."""
    _repo, sink, pool, sid = repo_and_sink
    try:
        bind_event_store(sink)
        publish_event("census", {"player_id": "p1", "round": 4}, component="mechanical")
        with pool.connection() as conn:
            row = tuple(
                conn.execute(
                    "SELECT event_seq, round, component FROM turn_telemetry WHERE session_id = %s",
                    (sid,),
                ).fetchone()
            )
        assert row == (None, 4, "mechanical")
    finally:
        bind_event_store(None)
