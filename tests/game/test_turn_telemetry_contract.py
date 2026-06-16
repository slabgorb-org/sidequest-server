# tests/game/test_turn_telemetry_contract.py
"""Characterization: pins the invariant the turn_telemetry sink rests on.

If any of these fail, the sink's transaction-mode + event_seq derivation
is unsound and the rest of the plan must not proceed.
"""

from sidequest.game.persistence import SqliteStore
from sidequest.telemetry import watcher_hub
from sidequest.telemetry.watcher_hub import bind_event_store


def _store(tmp_path) -> SqliteStore:
    return SqliteStore.open(str(tmp_path / "save.db"))


def test_bind_event_store_binds_the_same_conn_object(tmp_path):
    """The process-global the sink reads (_event_store._conn) is the SAME
    connection object the C2 turn transaction writes events/projection_cache
    through. connect.py passes the SAME store local to both
    bind_event_store(store) (handlers/connect.py ~:273) and EventLog(store)
    (~:819), so the bound global and the EventLog share one connection."""
    store = _store(tmp_path)
    try:
        bind_event_store(store)
        assert watcher_hub._event_store is store
        assert watcher_hub._event_store._conn is store._conn
    finally:
        bind_event_store(None)
        store.close()


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
