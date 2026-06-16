"""world_save table bootstrapping."""

from sidequest.game.persistence import SqliteStore


def test_world_save_table_created_on_init():
    store = SqliteStore.open_in_memory()
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='world_save'"
    ).fetchall()
    assert len(rows) == 1, "world_save table must be created by _init_schema"
