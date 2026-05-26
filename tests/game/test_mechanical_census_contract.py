"""Characterization: pins the invariant the mechanical census rests on.

R1: the C2 turn write txn is LOCAL to emitters.emit_event's `with conn:`
block. A publish_event issued inside that block (after the events INSERT,
before block exit) rides the C2 txn: in_transaction is True and the sink
attributes event_seq = MAX(seq) FROM events. If this fails, the census
cannot be made atomic with the turn — STOP and escalate.
"""

from sidequest.game.persistence import SqliteStore
from sidequest.telemetry.watcher_hub import bind_event_store, publish_event


def _store(tmp_path) -> SqliteStore:
    return SqliteStore.open(str(tmp_path / "save.db"))


def test_publish_inside_emit_style_block_rides_the_c2_txn(tmp_path):
    """Simulates emit_event's `with conn:` block: events INSERT first
    (tx.append_event via repo.transaction()), THEN a component='mechanical' publish. The
    census row must (a) attribute event_seq = the in-flight events row and
    (b) roll back with the turn."""
    store = _store(tmp_path)
    try:
        bind_event_store(store)
        conn = store._conn
        try:
            with conn:  # the emit_event C2 transaction
                conn.execute(
                    "INSERT INTO events (kind, payload_json, created_at) "
                    "VALUES ('NARRATION', '{}', 't')"
                )
                assert (
                    conn.in_transaction is True
                )  # guard: the first DML opened the C2 txn before we publish
                publish_event(
                    "census",
                    {"player_id": "p1", "round": 4},
                    component="mechanical",
                )
                events_seq = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
                inflight = [
                    tuple(r)
                    for r in conn.execute(
                        "SELECT event_seq, round, component, event_type FROM turn_telemetry"
                    ).fetchall()
                ]
                # rides the txn: event_seq = MAX(seq) of the in-flight event
                assert inflight == [(events_seq, 4, "mechanical", "census")]
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        # turn rolled back -> census rolled back atomically with it
        assert conn.execute("SELECT COUNT(*) FROM turn_telemetry").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        bind_event_store(None)
        store.close()


def test_publish_outside_any_block_does_not_ride_a_turn(tmp_path):
    """Sanity foil: a mechanical publish with no open txn takes its own
    short txn and event_seq is NULL (NOT attributed to a turn). Proves the
    in-txn attribution in the test above is the meaningful signal."""
    store = _store(tmp_path)
    try:
        bind_event_store(store)
        publish_event("census", {"player_id": "p1", "round": 4}, component="mechanical")
        row = tuple(
            store._conn.execute("SELECT event_seq, round, component FROM turn_telemetry").fetchone()
        )
        assert row == (None, 4, "mechanical")
    finally:
        bind_event_store(None)
        store.close()
