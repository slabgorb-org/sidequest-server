"""Session lifecycle over Postgres (ADR-115). Absorbs session_meta + games."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg_pool import ConnectionPool

# Per-session tables that ``init_session()`` clears on reinit — matches the
# SQLite ``_PER_SLOT_TABLES`` (persistence.py:40-47) exactly, since the migration
# must not change game logic. ``world_save`` (singleton hub state),
# ``scenario_archive`` (global), ``turn_telemetry``, and ``location_promotions``
# are global-lifecycle and intentionally NOT cleared on reinit — they survive.
# The sessions row itself (genre/world/mode/slug) is preserved (not in this list).
#
# Order matters: ``projection_cache`` carries a foreign key to ``events`` — the
# FK child must clear before its parent.
_PER_SESSION_TABLES = (
    "projection_cache",
    "events",
    "game_state",
    "narrative_log",
    "scrapbook_entries",
    "lore_fragments",
)


@dataclass(frozen=True)
class GameRow:
    slug: str
    mode: str
    genre_slug: str
    world_slug: str
    claude_session_id: str | None
    created_at: str


def resolve_session_id(pool: ConnectionPool, *, slug: str) -> int | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE session_slug = %s", (slug,)
        ).fetchone()
    return int(row[0]) if row else None


def ensure_session(
    pool: ConnectionPool, *, slug: str, mode: str, genre_slug: str, world_slug: str
) -> int:
    """Create the sessions row if absent (idempotent on session_slug); return session_id."""
    now = datetime.now(tz=UTC).isoformat()
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO sessions (session_slug, mode, genre_slug, world_slug, created_at, last_played)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_slug) DO UPDATE SET last_played = excluded.last_played
            RETURNING session_id
            """,
            (slug, mode, genre_slug, world_slug, now, now),
        ).fetchone()
    return int(row[0])


def get_game(pool: ConnectionPool, *, slug: str) -> GameRow | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT session_slug, mode, genre_slug, world_slug, claude_session_id, created_at "
            "FROM sessions WHERE session_slug = %s",
            (slug,),
        ).fetchone()
    if row is None:
        return None
    return GameRow(
        slug=row[0],
        mode=row[1],
        genre_slug=row[2],
        world_slug=row[3],
        claude_session_id=row[4],
        created_at=row[5],
    )


def init_session(pool: ConnectionPool, *, session_id: int) -> None:
    """Slot reinitialization — clear every per-session row for a fresh start.

    The sessions row (genre/world/mode/slug) is preserved; the cascade is NOT
    used here because we keep the session identity and only wipe its content.
    """
    with pool.connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM sessions WHERE session_id = %s FOR UPDATE", (session_id,))
        for table in _PER_SESSION_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE session_id = %s", (session_id,))
