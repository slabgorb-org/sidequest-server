"""ADR-115 TG-E: single-save ad-hoc SQLite→Postgres importer.

A one-shot importer for ONE real save. Descoped 2026-05-26: NO intermediate
JSON bundle, NO argparse CLI, NO --dry-run/--save-dir flags, NO whole-corpus
loop. Keeps the load-bearing correctness requirements:

  - FK-ordered insert (sessions → per-session tables → projection_cache).
  - created_at/last_played isoformat normalization (PgForensicReader.build_timeline
    lexically sorts created_at and dropped its _NORM_EV_TS normalization, so
    a mixed space/'T' separator would mis-bound rounds — see memory
    project_pg_importer_created_at_normalization).
  - A round-trip check (the importer returns a per-table ImportSummary).
  - Preserve the original: the SQLite save is opened READ-ONLY + IMMUTABLE;
    it is never written, checkpointed, or copy-then-mutated.

Raw FK-ordered INSERTs preserve source seq/round/payload/content verbatim.
The import is NOT routed through the Pg*Store write methods — those stamp a
fresh ``created_at = now`` which would destroy the historical timeline. The
whole import runs in ONE transaction so a partial failure rolls back.

No Silent Fallbacks, No Stubs: an empty or missing source table simply
contributes 0 to its count; a genuinely-malformed source raises.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class ImportSummary:
    """Per-table inserted row counts for one imported save."""

    sessions: int = 0
    game_state: int = 0
    narrative_log: int = 0
    scrapbook_entries: int = 0
    events: int = 0
    turn_telemetry: int = 0
    projection_cache: int = 0


def _norm_ts(value: str) -> str:
    """Normalize a text timestamp to T-isoformat.

    ``datetime.fromisoformat`` accepts the SQLite space separator
    (``YYYY-MM-DD HH:MM:SS``) and re-emits with a ``T``; it is idempotent on
    already-T values, preserving any tz offset and microseconds. This is the
    load-bearing correctness step (see module docstring). Never alters
    seq/round/payload/content.
    """
    return datetime.fromisoformat(value).isoformat()


def import_sqlite_save(sqlite_path: str, pool: ConnectionPool) -> ImportSummary:
    """Import ONE SQLite save into Postgres ``pool``; return per-table counts.

    The SQLite file is opened READ-ONLY + IMMUTABLE and never mutated. The
    Postgres side runs inside ONE transaction (atomic: a partial failure rolls
    back the whole import).
    """
    # READ-ONLY + IMMUTABLE: the original save is precious and must never be
    # written, checkpointed, or copy-then-mutated.
    uri = f"file:{sqlite_path}?mode=ro&immutable=1"
    src = sqlite3.connect(uri, uri=True)
    src.row_factory = sqlite3.Row
    try:
        meta = src.execute(
            "SELECT genre_slug, world_slug, created_at, last_played, schema_version "
            "FROM session_meta WHERE id = 1"
        ).fetchone()
        if meta is None:
            raise ValueError(f"{sqlite_path}: session_meta has no id=1 row")
        game = src.execute("SELECT slug, mode, claude_session_id FROM games").fetchone()
        if game is None:
            raise ValueError(f"{sqlite_path}: games table is empty")

        # Empty string claude_session_id → store as empty string verbatim.
        # We faithfully preserve the source value (the sessions column is
        # NULLABLE, but the source carries '' not NULL, so we keep '').
        claude_session_id = game["claude_session_id"]

        narr_rows = src.execute(
            "SELECT round_number, author, content, tags, created_at FROM narrative_log ORDER BY id"
        ).fetchall()
        scrb_rows = src.execute(
            "SELECT turn_id, scene_title, scene_type, location, image_url, "
            "narrative_excerpt, world_facts, npcs_present, render_status, created_at "
            "FROM scrapbook_entries ORDER BY id"
        ).fetchall()
        ev_rows = src.execute(
            "SELECT seq, kind, payload_json, created_at FROM events ORDER BY seq"
        ).fetchall()
        tel_rows = src.execute(
            "SELECT event_seq, round, ts, component, event_type, payload_json "
            "FROM turn_telemetry ORDER BY seq"
        ).fetchall()
        proj_rows = src.execute(
            "SELECT event_seq, player_id, include, payload_json "
            "FROM projection_cache ORDER BY event_seq, player_id"
        ).fetchall()
        gs = src.execute("SELECT snapshot_json, saved_at FROM game_state WHERE id = 1").fetchone()

        # No Silent Fallbacks: this descoped one-save importer covers exactly
        # the tables coyote_star-mp populates. Any OTHER source table that
        # carries rows (e.g. world_save, location_promotions, scenario_archive,
        # lore_fragments, dungeon_*) would be silently dropped — fail loud
        # instead, so a reuse against a richer save (beneath_sunden-mp has
        # dungeon data) is caught rather than losing campaign state.
        _handled = {
            "session_meta",
            "games",
            "game_state",
            "narrative_log",
            "scrapbook_entries",
            "events",
            "turn_telemetry",
            "projection_cache",
        }
        src_tables = {
            row[0]
            for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        for table in sorted(src_tables - _handled):
            count = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 — table name from sqlite_master, not user input
            if count:
                raise ValueError(
                    f"{sqlite_path}: table {table!r} has {count} rows the "
                    f"single-save importer does not handle (it covers only "
                    f"{sorted(_handled)}). Refusing to silently drop them."
                )
    finally:
        src.close()

    with pool.connection() as conn, conn.transaction():
        # 1. sessions (RETURNING session_id) — the FK for every per-session row.
        session_id = conn.execute(
            """
            INSERT INTO sessions (
                session_slug, mode, genre_slug, world_slug, claude_session_id,
                schema_version, created_at, last_played
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING session_id
            """,
            (
                game["slug"],
                game["mode"],
                meta["genre_slug"],
                meta["world_slug"],
                claude_session_id,
                meta["schema_version"],
                _norm_ts(meta["created_at"]),
                _norm_ts(meta["last_played"]),
            ),
        ).fetchone()[0]
        sessions_count = 1

        # 2a. game_state (one row; saved_at normalized).
        game_state_count = 0
        if gs is not None:
            conn.execute(
                "INSERT INTO game_state (session_id, snapshot_json, saved_at) VALUES (%s, %s, %s)",
                (session_id, gs["snapshot_json"], _norm_ts(gs["saved_at"])),
            )
            game_state_count = 1

        # 2b. narrative_log (IDENTITY id NOT inserted; created_at normalized).
        for r in narr_rows:
            conn.execute(
                "INSERT INTO narrative_log "
                "(session_id, round_number, author, content, tags, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    session_id,
                    r["round_number"],
                    r["author"],
                    r["content"],
                    r["tags"],
                    _norm_ts(r["created_at"]),
                ),
            )

        # 2c. scrapbook_entries (IDENTITY id NOT inserted; created_at normalized).
        for r in scrb_rows:
            conn.execute(
                "INSERT INTO scrapbook_entries "
                "(session_id, turn_id, scene_title, scene_type, location, image_url, "
                " narrative_excerpt, world_facts, npcs_present, render_status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    session_id,
                    r["turn_id"],
                    r["scene_title"],
                    r["scene_type"],
                    r["location"],
                    r["image_url"],
                    r["narrative_excerpt"],
                    r["world_facts"],
                    r["npcs_present"],
                    r["render_status"],
                    _norm_ts(r["created_at"]),
                ),
            )

        # 2d. events — seq is a plain BIGINT, PK(session_id, seq): PRESERVE it.
        # created_at normalized (already T-isoformat in the source; idempotent).
        for r in ev_rows:
            conn.execute(
                "INSERT INTO events (session_id, seq, kind, payload_json, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    session_id,
                    r["seq"],
                    r["kind"],
                    r["payload_json"],
                    _norm_ts(r["created_at"]),
                ),
            )

        # 2e. turn_telemetry — IDENTITY seq NOT inserted; preserve
        # event_seq/round/component/event_type/payload; ts normalized.
        for r in tel_rows:
            conn.execute(
                "INSERT INTO turn_telemetry "
                "(session_id, event_seq, round, ts, component, event_type, payload_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    session_id,
                    r["event_seq"],
                    r["round"],
                    _norm_ts(r["ts"]),
                    r["component"],
                    r["event_type"],
                    r["payload_json"],
                ),
            )

        # 3. projection_cache — FK (session_id, event_seq) → events: MUST be last.
        for r in proj_rows:
            conn.execute(
                "INSERT INTO projection_cache "
                "(session_id, event_seq, player_id, include, payload_json) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    session_id,
                    r["event_seq"],
                    r["player_id"],
                    r["include"],
                    r["payload_json"],
                ),
            )

    return ImportSummary(
        sessions=sessions_count,
        game_state=game_state_count,
        narrative_log=len(narr_rows),
        scrapbook_entries=len(scrb_rows),
        events=len(ev_rows),
        turn_telemetry=len(tel_rows),
        projection_cache=len(proj_rows),
    )


if __name__ == "__main__":
    # One-shot: import the single precious coyote_star-mp save into the pool
    # resolved from SIDEQUEST_DATABASE_URL. No argparse — TG-E is descoped to
    # exactly this one save. Run with the target DB exported in the env.
    from sidequest.game.db_pool import get_pool

    _REAL_SAVE = "/Users/slabgorb/.sidequest/saves/games/2026-05-17-coyote_star-mp/save.db"
    _summary = import_sqlite_save(_REAL_SAVE, get_pool())
    print(f"imported {_REAL_SAVE}: {_summary}")
