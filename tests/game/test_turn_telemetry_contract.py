# tests/game/test_turn_telemetry_contract.py
"""Characterization: pins the invariant the turn_telemetry sink rests on.

If any of these fail, the sink's transaction-mode + event_seq derivation
is unsound and the rest of the plan must not proceed.
"""

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.persistence import SqliteStore
from sidequest.game.pg import sessions
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.telemetry import watcher_hub
from sidequest.telemetry.watcher_hub import bind_event_store, publish_event


def _store(tmp_path) -> SqliteStore:
    return SqliteStore.open(str(tmp_path / "save.db"))


@pytest.fixture
def pg_sink(monkeypatch, migrated_db: str):
    """A PgTelemetrySink over a freshly-migrated throwaway PG session — the
    same kind of sink ``handlers/connect.py`` binds in production (D5)."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"sq_contract_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgTelemetrySink(pool, sid), pool, sid
    db_pool.close_pool()


def test_bind_event_store_binds_the_same_conn_object(pg_sink):
    """The process-global the out-of-frame publish path reads
    (``watcher_hub._telemetry_sink``) is the SAME TelemetrySink object passed
    to ``bind_event_store`` — and a tx-less publish routes through it.

    Post-ADR-115 D5: the in-frame census path no longer shares a connection via
    a global; it threads the open turn ``tx`` EXPLICITLY (emitters.emit_event →
    emit_mechanical_census). So the invariant the sink rests on is now binding
    identity + the out-of-frame ``record`` route, not a shared ``_conn``.
    ``handlers/connect.py`` (~:305) binds the PgTelemetrySink built for the
    session."""
    sink, pool, sid = pg_sink
    try:
        bind_event_store(sink)
        # Binding identity: the bound global IS the sink we passed.
        assert watcher_hub._telemetry_sink is sink  # noqa: SLF001
        # And a tx-less publish routes out-of-frame THROUGH that sink
        # (event_seq NULL), proving the binding is the live write path.
        publish_event(
            "state_transition",
            {"field": "intent", "round": 2},
            component="intent",
        )
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT event_seq, component FROM turn_telemetry "
                "WHERE session_id = %s AND event_type = %s",
                (sid, "state_transition"),
            ).fetchone()
        assert row is not None, "tx-less publish must persist through the bound sink"
        assert row[0] is None  # out-of-frame -> NULL event_seq
        assert row[1] == "intent"
    finally:
        bind_event_store(None)


def test_deferred_isolation_in_transaction_invariant(tmp_path):
    """Default deferred isolation: SELECT does NOT open a write txn; the
    first DML flips in_transaction True; it stays True until commit; a
    `with conn:` block is True only after its first DML and False after
    the block. This is the exact signal the sink branches on."""
    store = _store(tmp_path)
    try:
        conn = store._conn
        assert conn.in_transaction is False  # quiescent
        conn.execute("SELECT 1").fetchone()
        assert conn.in_transaction is False  # SELECT does not begin a write txn
        with conn:
            conn.execute(
                "INSERT INTO events (kind, payload_json, created_at) "
                "VALUES ('NARRATION', '{}', 't')"
            )
            assert conn.in_transaction is True  # first DML flipped it
            seq = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
            # the in-flight row is visible within the txn
            assert seq == 1  # fresh tmp_path db — first events row gets seq=1
        assert conn.in_transaction is False  # `with conn:` committed + closed txn
    finally:
        store.close()
