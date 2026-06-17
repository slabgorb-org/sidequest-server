"""ADR-115 TG-E: single-save SQLite→Postgres importer round-trip tests.

Two tests:
  1. Synthetic-fixture test (always runs): a tiny hand-built SQLite save with a
     SPACE-format narrative created_at and a T-format event created_at; asserts
     the ImportSummary counts, that the narrative created_at was normalized to
     T-isoformat, that events.seq is preserved verbatim, and that the
     projection_cache FK row landed.
  2. Real-save round-trip (skipif the real save is absent): imports the precious
     141-turn coyote_star-mp save READ-ONLY/IMMUTABLE, asserts exact per-table
     counts + the sessions row, asserts NO inserted created_at carries a
     date/time space separator, then runs PgForensicReader.build_timeline and
     asserts ascending round order (the normalization payoff — mixed
     space/T formats would mis-bound rounds via lexical sort).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg
import pytest

from sidequest.game import db_pool
from sidequest.game.importer import ImportSummary, import_sqlite_save
from sidequest.game.pg.forensic import PgForensicReader

REAL_SAVE = "/Users/slabgorb/.sidequest/saves/games/2026-05-17-coyote_star-mp/save.db"


@pytest.fixture
def pool(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to the per-worker throwaway PG db, clean per test.

    Copies the _pg_isolation pattern from test_turn_telemetry_wiring.py: point
    SIDEQUEST_DATABASE_URL at the migrated (plain-scheme) db, reset the pool,
    TRUNCATE all tables so each test starts empty. Tests target the test PG DB
    only — NEVER the production `sidequest` DB.
    """
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    p = db_pool.get_pool()
    yield p
    db_pool.close_pool()


def _build_synthetic_save(path: Path) -> None:
    """Hand-build a minimal SQLite save with the source schema.

    Includes one SPACE-format narrative created_at and one T-format event
    created_at so the normalization assertion is non-vacuous.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE session_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,
            created_at TEXT NOT NULL, last_played TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE games (
            slug TEXT PRIMARY KEY,
            mode TEXT NOT NULL CHECK (mode IN ('solo', 'multiplayer')),
            genre_slug TEXT NOT NULL, world_slug TEXT NOT NULL,
            claude_session_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE game_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            snapshot_json TEXT NOT NULL, saved_at TEXT NOT NULL
        );
        CREATE TABLE narrative_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_number INTEGER NOT NULL, author TEXT NOT NULL,
            content TEXT NOT NULL, tags TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE scrapbook_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id INTEGER NOT NULL, scene_title TEXT, scene_type TEXT,
            location TEXT NOT NULL, image_url TEXT,
            narrative_excerpt TEXT NOT NULL,
            world_facts TEXT NOT NULL DEFAULT '[]',
            npcs_present TEXT NOT NULL DEFAULT '[]',
            render_status TEXT NOT NULL DEFAULT 'rendered',
            created_at TEXT NOT NULL
        );
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE turn_telemetry (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_seq INTEGER, round INTEGER, ts TEXT NOT NULL,
            component TEXT NOT NULL, event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE projection_cache (
            event_seq INTEGER NOT NULL, player_id TEXT NOT NULL,
            include INTEGER NOT NULL, payload_json TEXT,
            PRIMARY KEY (event_seq, player_id),
            FOREIGN KEY (event_seq) REFERENCES events(seq)
        );
        """
    )
    conn.execute(
        "INSERT INTO session_meta (id, genre_slug, world_slug, created_at, "
        "last_played, schema_version) VALUES (1, ?, ?, ?, ?, 1)",
        ("space_opera", "coyote_star", "2026-05-17 18:04:30", "2026-05-24 17:12:06"),
    )
    conn.execute(
        "INSERT INTO games (slug, mode, genre_slug, world_slug, claude_session_id, "
        "created_at) VALUES (?, 'multiplayer', ?, ?, '', ?)",
        ("synthetic-save", "space_opera", "coyote_star", "2026-05-17 18:04:30"),
    )
    conn.execute(
        "INSERT INTO game_state (id, snapshot_json, saved_at) VALUES (1, ?, ?)",
        ('{"genre_slug": "space_opera"}', "2026-05-24 17:12:06"),
    )
    # narrative: SPACE-format created_at, two rounds (ascending).
    conn.execute(
        "INSERT INTO narrative_log (round_number, author, content, tags, created_at) "
        "VALUES (1, 'narrator', 'Round one.', NULL, ?)",
        ("2026-05-17 18:08:19",),
    )
    conn.execute(
        "INSERT INTO narrative_log (round_number, author, content, tags, created_at) "
        "VALUES (2, 'narrator', 'Round two.', NULL, ?)",
        ("2026-05-17 18:20:00",),
    )
    # events: T-isoformat created_at; seq 1 and 2 (preserve verbatim).
    conn.execute(
        "INSERT INTO events (seq, kind, payload_json, created_at) VALUES (1, 'NARRATION', '{}', ?)",
        ("2026-05-17T18:06:48.217822+00:00",),
    )
    conn.execute(
        "INSERT INTO events (seq, kind, payload_json, created_at) VALUES (2, 'NARRATION', '{}', ?)",
        ("2026-05-17T18:19:00.000000+00:00",),
    )
    # projection_cache FK → events(seq=1)
    conn.execute(
        "INSERT INTO projection_cache (event_seq, player_id, include, payload_json) "
        "VALUES (1, 'alice', 1, '{}')",
    )
    conn.commit()
    conn.close()


def test_synthetic_save_roundtrip(pool, tmp_path: Path) -> None:
    sqlite_path = tmp_path / "synthetic.db"
    _build_synthetic_save(sqlite_path)

    summary = import_sqlite_save(str(sqlite_path), pool)

    assert isinstance(summary, ImportSummary)
    assert summary.sessions == 1
    assert summary.game_state == 1
    assert summary.narrative_log == 2
    assert summary.events == 2
    assert summary.scrapbook_entries == 0
    assert summary.turn_telemetry == 0
    assert summary.projection_cache == 1

    with pool.connection() as conn:
        sid = conn.execute(
            "SELECT session_id FROM sessions WHERE session_slug = %s",
            ("synthetic-save",),
        ).fetchone()[0]

        # (a) SPACE-format narrative created_at rewritten to T-isoformat.
        narr_ts = conn.execute(
            "SELECT created_at FROM narrative_log WHERE session_id = %s ORDER BY round_number",
            (sid,),
        ).fetchall()
        for (ts,) in narr_ts:
            assert "T" in ts, f"narrative created_at not T-normalized: {ts!r}"
            assert " " not in ts, f"narrative created_at has space separator: {ts!r}"

        # (b) events.seq preserved verbatim.
        seqs = [
            r[0]
            for r in conn.execute(
                "SELECT seq FROM events WHERE session_id = %s ORDER BY seq", (sid,)
            ).fetchall()
        ]
        assert seqs == [1, 2]

        # (c) projection_cache FK row present.
        proj = conn.execute(
            "SELECT event_seq, player_id, include FROM projection_cache WHERE session_id = %s",
            (sid,),
        ).fetchall()
        assert proj == [(1, "alice", 1)]


def test_unhandled_nonempty_table_fails_loud(pool, tmp_path: Path) -> None:
    """No Silent Fallbacks: a source table the single-save importer does not
    handle (here world_save) carrying rows must raise, not be silently dropped.
    """
    sqlite_path = tmp_path / "with_world_save.db"
    _build_synthetic_save(sqlite_path)
    conn = sqlite3.connect(str(sqlite_path))
    conn.executescript(
        "CREATE TABLE world_save (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "payload_json TEXT NOT NULL, saved_at TEXT NOT NULL);"
        "INSERT INTO world_save (id, payload_json, saved_at) "
        "VALUES (1, '{}', '2026-05-17 18:04:30');"
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="world_save"):
        import_sqlite_save(str(sqlite_path), pool)


@pytest.mark.skipif(
    not Path(REAL_SAVE).exists(),
    reason=f"real coyote_star-mp save absent at {REAL_SAVE}",
)
def test_real_save_roundtrip(pool) -> None:
    summary = import_sqlite_save(REAL_SAVE, pool)

    assert summary.sessions == 1
    assert summary.game_state == 1
    assert summary.events == 236
    assert summary.narrative_log == 234
    assert summary.scrapbook_entries == 118
    assert summary.turn_telemetry == 59
    assert summary.projection_cache == 706

    with pool.connection() as conn:
        srow = conn.execute(
            "SELECT session_id, session_slug, mode, genre_slug, world_slug FROM sessions"
        ).fetchone()
        sid, slug, mode, genre, world = srow
        assert slug == "2026-05-17-coyote_star-mp"
        assert mode == "multiplayer"
        assert genre == "space_opera"
        assert world == "coyote_star"

        # No inserted created_at carries a date/time space separator.
        for table in ("narrative_log", "scrapbook_entries", "events"):
            rows = conn.execute(
                f"SELECT created_at FROM {table} WHERE session_id = %s", (sid,)
            ).fetchall()
            for (ts,) in rows:
                assert "T" in ts, f"{table}.created_at missing T: {ts!r}"
                assert " " not in ts, f"{table}.created_at has space separator: {ts!r}"

    # The normalization payoff: build_timeline must return rounds in ascending
    # order. Mixed space/T formats would mis-bound via lexical sort.
    reader = PgForensicReader(pool)
    timeline = reader.build_timeline(sid)
    assert timeline, "build_timeline returned no rounds"
    rounds = [entry["round"] for entry in timeline]
    assert rounds == sorted(rounds), f"rounds not ascending: {rounds}"
