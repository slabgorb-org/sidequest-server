"""projection_cache table bootstrapping."""

from pathlib import Path

from sidequest.game.persistence import SqliteStore


def test_projection_cache_table_created(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "test.db")
    with store._conn:
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='projection_cache'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "projection_cache"


def test_projection_cache_columns(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "test.db")
    with store._conn:
        cols = store._conn.execute("PRAGMA table_info(projection_cache)").fetchall()
    names = {c[1] for c in cols}
    assert names == {"event_seq", "player_id", "include", "payload_json"}


def test_projection_cache_primary_key(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "test.db")
    with store._conn:
        cols = store._conn.execute("PRAGMA table_info(projection_cache)").fetchall()
    pk_cols = {c[1] for c in cols if c[5] > 0}
    assert pk_cols == {"event_seq", "player_id"}
