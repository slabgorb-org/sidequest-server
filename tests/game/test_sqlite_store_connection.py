from __future__ import annotations

import sqlite3

from sidequest.dungeon.persistence import DungeonStore
from sidequest.game.persistence import SqliteStore


def test_connection_returns_the_live_shared_conn() -> None:
    store = SqliteStore.open_in_memory()
    conn = store.connection()
    assert isinstance(conn, sqlite3.Connection)
    # Same object — one connection, never a copy (spec §7.5).
    assert conn is store._conn


def test_dungeonstore_shares_sqlitestore_connection() -> None:
    store = SqliteStore.open_in_memory()
    ds = DungeonStore(store.connection())  # must NOT raise the Path guard
    ds.ensure_schema()
    rows = (
        store.connection()
        .execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dungeon_map'")
        .fetchall()
    )
    assert rows, (
        "DungeonStore.ensure_schema did not write through the shared "
        "connection — connection() handed out a different conn (spec §7.5 "
        "one-transaction contract broken)"
    )
