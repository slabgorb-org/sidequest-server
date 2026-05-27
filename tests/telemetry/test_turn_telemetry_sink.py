import json
import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.telemetry.watcher_hub import bind_event_store, publish_event


@pytest.fixture(autouse=True)
def _clear_event_store_binding():
    """Guarantee the process-global telemetry-sink binding is cleared after
    every test, even if a test raises before its own finally — keeps the
    tests in this module order-independent as more are appended."""
    yield
    bind_event_store(None)


@pytest.fixture
def repo_and_sink(monkeypatch, migrated_db: str):
    """A migrated throwaway PG session with a PgSaveRepository (for the open
    turn ``tx``) and a PgTelemetrySink (the out-of-frame sink). Mirrors the
    canonical ``tests/persistence/test_pg_telemetry.py`` fixture."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"sq_sink_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    repo = PgSaveRepository(pool, session_id=sid)
    sink = PgTelemetrySink(pool, sid)
    yield repo, sink, pool, sid
    db_pool.close_pool()


def test_publish_outside_txn_writes_row_with_null_event_seq(repo_and_sink):
    """A publish with no open turn ``tx`` goes out-of-frame through the bound
    ``TelemetrySink.record`` with a NULL event_seq."""
    _repo, sink, pool, sid = repo_and_sink
    bind_event_store(sink)
    publish_event(
        "state_transition",
        {"field": "intent", "label": "explore", "round": 3},
        component="intent",
    )
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT event_seq, round, component, event_type, payload_json "
            "FROM turn_telemetry WHERE session_id = %s",
            (sid,),
        ).fetchall()
    assert len(rows) == 1
    event_seq, rnd, component, event_type, payload = rows[0]
    assert event_seq is None  # fired outside any turn (C2) transaction
    assert rnd == 3  # best-effort from fields["round"]
    assert component == "intent"
    assert event_type == "state_transition"
    assert json.loads(payload) == {
        "field": "intent",
        "label": "explore",
        "round": 3,
    }


def test_publish_inside_open_txn_joins_it_and_attributes_event_seq(repo_and_sink):
    """When a turn ``tx`` is threaded through, the telemetry row JOINS that
    transaction (same connection) and is attributed event_seq = the in-flight
    events row. Atomicity: rolling back the turn rolls back the telemetry too.

    The split is decided by the EXPLICIT ``tx`` parameter (ADR-115 D5) — never
    by sniffing connection state."""
    repo, sink, pool, sid = repo_and_sink
    bind_event_store(sink)

    captured_seq: list[int] = []
    with pytest.raises(RuntimeError, match="force rollback"), repo.transaction() as tx:
        ev = tx.append_event(kind="NARRATION", payload_json="{}")
        publish_event(
            "state_transition",
            {"field": "projection", "decision": "include"},
            component="projection",
            tx=tx,
            event_seq=ev.seq,
        )
        captured_seq.append(ev.seq)
        # in-flight: the row is visible WITHIN the open tx (same connection)
        # and attributed event_seq = the in-flight events row.
        inflight = [
            r[0]
            for r in tx._conn.execute(  # noqa: SLF001 — read the in-flight tx state
                "SELECT event_seq FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
                (sid, ev.seq),
            ).fetchall()
        ]
        assert inflight == [ev.seq]  # rides the txn, attributed to the event's seq
        raise RuntimeError("force rollback")

    seq = captured_seq[0]
    # turn rolled back -> telemetry + event rolled back atomically with it
    with pool.connection() as conn:
        trow = conn.execute(
            "SELECT event_seq FROM turn_telemetry WHERE session_id = %s AND event_seq = %s",
            (sid, seq),
        ).fetchone()
        erow = conn.execute(
            "SELECT seq FROM events WHERE session_id = %s AND seq = %s",
            (sid, seq),
        ).fetchone()
    assert trow is None, "rolled-back telemetry row must not persist"
    assert erow is None, "rolled-back event must not persist"


def test_no_store_bound_is_noop_not_error():
    bind_event_store(None)  # legacy/in-memory session: no durable save
    publish_event("x", {"a": 1}, component="c")  # must not raise


def test_round_absent_or_non_int_is_stored_null(repo_and_sink):
    _repo, sink, pool, sid = repo_and_sink
    bind_event_store(sink)
    publish_event("e", {"no_round_here": True}, component="c")
    publish_event("e", {"round": "not-an-int"}, component="c")
    with pool.connection() as conn:
        rounds = [
            r[0]
            for r in conn.execute(
                "SELECT round FROM turn_telemetry WHERE session_id = %s ORDER BY seq",
                (sid,),
            ).fetchall()
        ]
    assert rounds == [None, None]


def test_pydantic_rootmodel_in_payload_serializes_via_tolerant_default(repo_and_sink):
    """A Pydantic RootModel (NonBlankString) inside fields must not crash
    the sink — the tolerant `_json_default` collapses it to its .root value.
    Regression for 2026-05-18 MP playtest: footnote forwards carry
    NonBlankString summaries; default json.dumps raised TypeError and
    every footnote turn lost telemetry."""
    from sidequest.protocol.types import NonBlankString

    _repo, sink, pool, sid = repo_and_sink
    bind_event_store(sink)
    publish_event(
        "state_transition",
        {
            "field": "footnote",
            "summaries": [NonBlankString(root="silver-salts pales the smear")],
            "round": 5,
        },
        component="footnotes",
    )
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM turn_telemetry WHERE session_id = %s",
            (sid,),
        ).fetchall()
    assert len(rows) == 1  # row persisted, not dropped
    payload = json.loads(rows[0][0])
    assert payload["summaries"] == ["silver-salts pales the smear"]
    assert payload["field"] == "footnote"


def test_concurrent_publishers_dont_lose_rows(repo_and_sink):
    """Two threads publishing out-of-frame telemetry against the same sink
    must not drop rows. Regression for 2026-05-18 MP playtest: concurrent turn
    telemetry from Laverne+Shirley collided on the saves connection and warned
    `database is locked` for every census/trope_census event. The PG pool
    gives each publisher its own short session_tx, so no drops."""
    import threading

    _repo, sink, pool, sid = repo_and_sink
    bind_event_store(sink)
    N = 20  # per-thread publish count
    errors: list[BaseException] = []

    def burst(label: str) -> None:
        try:
            for i in range(N):
                publish_event(
                    "state_transition",
                    {"field": "census", "label": label, "i": i},
                    component="mechanical",
                )
        except BaseException as exc:  # pragma: no cover — surfaces in assert
            errors.append(exc)

    t1 = threading.Thread(target=burst, args=("laverne",))
    t2 = threading.Thread(target=burst, args=("shirley",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors
    # Every publish must yield exactly one row — no drops.
    with pool.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry WHERE session_id = %s",
            (sid,),
        ).fetchone()[0]
    assert count == 2 * N


def test_sink_failure_logs_loudly_and_does_not_crash_the_turn(repo_and_sink, caplog):
    """A forced sink error must produce a loud turn_telemetry.sink_failed
    WARNING and return — publish_event still completes normally."""
    _repo, _sink, _pool, _sid = repo_and_sink

    class _ExplodingSink:
        def record(self, **_kwargs) -> None:
            raise RuntimeError("forced sink failure")

        def append_encounter_event(self, **_kwargs):  # pragma: no cover - unused here
            raise RuntimeError("forced sink failure")

    bind_event_store(_ExplodingSink())
    with caplog.at_level("WARNING"):
        publish_event("state_transition", {"field": "x"}, component="trope")  # must NOT raise
    assert "turn_telemetry.sink_failed" in caplog.text
    assert "component=trope event_type=state_transition" in caplog.text
