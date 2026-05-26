"""Initial unified schema migration applies and has the ADR-115 shape.

Behavior/introspection assertions only — never a source-text grep
(CLAUDE.md: No Source-Text Wiring Tests). `migrated_db` already ran
`alembic upgrade head`, so reaching it at all proves the migration applied.
"""

from __future__ import annotations

import psycopg
import pytest

ALL_TABLES = {
    "sessions",
    "game_state",
    "narrative_log",
    "lore_fragments",
    "scenario_archive",
    "scrapbook_entries",
    "events",
    "projection_cache",
    "world_save",
    "turn_telemetry",
    "location_promotions",
    "dungeon_map",
    "dungeon_edge",
    "dungeon_frontier",
    "dungeon_mutation_overlay",
    "dungeon_complication_ledger",
    "dungeon_meta",
}

PER_SESSION_TABLES = ALL_TABLES - {"sessions"}


def _columns(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def test_all_tables_exist(pg_conn: psycopg.Connection) -> None:
    rows = pg_conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    present = {r[0] for r in rows}
    assert ALL_TABLES <= present, f"missing tables: {ALL_TABLES - present}"


def test_sessions_has_surrogate_and_natural_key(pg_conn: psycopg.Connection) -> None:
    cols = _columns(pg_conn, "sessions")
    assert cols["session_id"] == "bigint"
    assert "session_slug" in cols
    # session_slug is UNIQUE
    uniq = pg_conn.execute(
        """
        SELECT 1 FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
        WHERE tc.table_name = 'sessions' AND tc.constraint_type = 'UNIQUE'
          AND ccu.column_name = 'session_slug'
        """
    ).fetchone()
    assert uniq is not None


@pytest.mark.parametrize("table", sorted(PER_SESSION_TABLES))
def test_every_per_session_table_has_session_id_fk(
    pg_conn: psycopg.Connection, table: str
) -> None:
    cols = _columns(pg_conn, table)
    assert cols.get("session_id") == "bigint", f"{table} missing session_id bigint"
    fk = pg_conn.execute(
        """
        SELECT ccu.table_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = %s
          AND kcu.column_name = 'session_id'
        """,
        (table,),
    ).fetchone()
    assert fk is not None and fk[0] == "sessions", f"{table}.session_id must FK -> sessions"


def test_events_pk_is_session_id_seq(pg_conn: psycopg.Connection) -> None:
    pk_cols = pg_conn.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        WHERE tc.table_name = 'events' AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """
    ).fetchall()
    assert [r[0] for r in pk_cols] == ["session_id", "seq"]


def test_projection_cache_fk_targets_composite_events_pk(pg_conn: psycopg.Connection) -> None:
    # Inserting a projection row for a non-existent (session_id, event_seq)
    # must be rejected by the composite FK.
    sid = pg_conn.execute(
        "INSERT INTO sessions (session_slug, genre_slug, world_slug, mode, created_at, last_played) "
        "VALUES ('t', 'g', 'w', 'solo', '2026-01-01', '2026-01-01') RETURNING session_id"
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_conn.execute(
            "INSERT INTO projection_cache (session_id, event_seq, player_id, include, payload_json) "
            "VALUES (%s, 999, 'p1', 1, NULL)",
            (sid,),
        )


def test_cascade_delete_removes_child_rows(pg_conn: psycopg.Connection) -> None:
    sid = pg_conn.execute(
        "INSERT INTO sessions (session_slug, genre_slug, world_slug, mode, created_at, last_played) "
        "VALUES ('c', 'g', 'w', 'solo', '2026-01-01', '2026-01-01') RETURNING session_id"
    ).fetchone()[0]
    pg_conn.execute(
        "INSERT INTO events (session_id, seq, kind, payload_json, created_at) "
        "VALUES (%s, 1, 'k', '{}', '2026-01-01')",
        (sid,),
    )
    pg_conn.execute("DELETE FROM sessions WHERE session_id = %s", (sid,))
    remaining = pg_conn.execute(
        "SELECT count(*) FROM events WHERE session_id = %s", (sid,)
    ).fetchone()[0]
    assert remaining == 0


def test_mask_is_bytea(pg_conn: psycopg.Connection) -> None:
    assert _columns(pg_conn, "dungeon_map")["mask"] == "bytea"
