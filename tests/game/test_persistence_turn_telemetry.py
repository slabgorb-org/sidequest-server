# tests/game/test_persistence_turn_telemetry.py
from sidequest.game.persistence import SqliteStore


def test_turn_telemetry_table_and_indexes_exist_on_fresh_save(tmp_path):
    store = SqliteStore.open(str(tmp_path / "save.db"))
    try:
        conn = store._conn
        cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(turn_telemetry)").fetchall()}
        assert cols == {
            "seq": "INTEGER",
            "event_seq": "INTEGER",
            "round": "INTEGER",
            "ts": "TEXT",
            "component": "TEXT",
            "event_type": "TEXT",
            "payload_json": "TEXT",
        }
        notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(turn_telemetry)").fetchall()}
        assert notnull == {
            "seq": 0,  # AUTOINCREMENT PK — sqlite reports notnull=0
            "event_seq": 0,  # nullable: out-of-turn publishes
            "round": 0,  # nullable: best-effort from fields["round"]
            "ts": 1,
            "component": 1,
            "event_type": 1,
            "payload_json": 1,
        }
        idx = {
            r[1]
            for r in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='turn_telemetry'"
            ).fetchall()
        }
        assert "idx_turn_telemetry_round" in idx
        assert "idx_turn_telemetry_event_seq" in idx
        # seq is AUTOINCREMENT PK: not supplied on INSERT, read via lastrowid
        cur = conn.execute(
            "INSERT INTO turn_telemetry "
            "(event_seq, round, ts, component, event_type, payload_json) "
            "VALUES (NULL, NULL, 't', 'c', 'e', '{}')"
        )
        assert cur.lastrowid == 1
    finally:
        store.close()
