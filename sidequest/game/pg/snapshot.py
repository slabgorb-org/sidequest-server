"""Postgres game_state + world_save adapter (ADR-115 A4).

Faithful port of SqliteStore.save / load / load_world_save / save_world_save
from sidequest.game.persistence.  Serialization form is identical —
``snapshot.model_dump_json()`` on save, ``GameSnapshot.model_validate(migrated)``
on load — so save files from either backend are interchangeable at the JSON
layer.

SQLite-only mechanics that are retired here:
  - PRAGMA wal_checkpoint(TRUNCATE)  — no WAL in Postgres
  - .bak / .canonicalize.bak copies  — no filesystem artefact to back up
  - SAVE_WRITE_LOCK                   — replaced by per-session row lock in
                                        session_tx (_conn.py)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from sidequest.game.migrations import migrate_legacy_snapshot
from sidequest.game.persistence import (
    SavedSession,
    SaveSchemaIncompatibleError,
    SessionMeta,
    _generate_recap,
    _parse_rfc3339,
)
from sidequest.game.pg._conn import session_tx
from sidequest.game.session import GameSnapshot, NarrativeEntry
from sidequest.game.world_save import WorldSave
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)

# Sentinel path used where SqliteStore would pass ``self._path`` — Postgres has
# no save file path, so we use a stable placeholder for error messages.
_PG_SAVE_PATH = Path("<postgres>")


class PgSnapshotStore:
    """Postgres adapter for game_state + world_save (ADR-115 A4).

    Maps directly onto the ``game_state`` and ``world_save`` tables whose
    schema is defined in alembic/versions/0001_initial_unified_schema.py.

    All writes run inside ``session_tx`` (which takes the per-session row
    lock and commits on clean exit) — same serialisation guarantee as
    SAVE_WRITE_LOCK on the SQLite side, but scoped to one session rather
    than the whole process.

    ``recent_narrative`` is read inline (no PgNarrativeStore yet — that is
    A5).  The SQL mirrors ``SqliteStore.recent_narrative`` exactly.
    """

    def __init__(self, pool: ConnectionPool, *, session_id: int) -> None:
        self._pool = pool
        self._session_id = session_id

    # ------------------------------------------------------------------
    # game_state
    # ------------------------------------------------------------------

    def save_snapshot(self, snapshot: GameSnapshot) -> None:
        """Serialize and upsert ``snapshot`` into ``game_state``.

        Mirrors ``SqliteStore.save`` exactly:
          1. Stamp ``last_saved_at`` on a copy.
          2. ``model_dump_json()`` for the wire form.
          3. Upsert into ``game_state`` keyed by ``session_id``.
          4. Update ``sessions.last_played``.
          5. Emit ``state_transition / snapshot_saved`` watcher event.

        Both DB writes run in one ``session_tx`` (row-locked, atomic).
        """
        now = datetime.now(tz=UTC)
        snapshot_copy = snapshot.model_copy(update={"last_saved_at": now})
        state_json = snapshot_copy.model_dump_json()
        now_str = now.isoformat()

        with session_tx(self._pool, self._session_id) as conn:
            conn.execute(
                """
                INSERT INTO game_state (session_id, snapshot_json, saved_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE
                    SET snapshot_json = excluded.snapshot_json,
                        saved_at      = excluded.saved_at
                """,
                (self._session_id, state_json, now_str),
            )
            conn.execute(
                "UPDATE sessions SET last_played = %s WHERE session_id = %s",
                (now_str, self._session_id),
            )

        _watcher_publish(
            "state_transition",
            {
                "field": "save",
                "op": "snapshot_saved",
                "genre_slug": snapshot.genre_slug,
                "world_slug": snapshot.world_slug,
                "round": snapshot.turn_manager.round if snapshot.turn_manager else 0,
                "interaction": snapshot.turn_manager.interaction if snapshot.turn_manager else 0,
                "character_count": len(snapshot.characters),
                "npc_count": len(snapshot.npcs),
                "byte_size": len(state_json),
                "save_path": str(_PG_SAVE_PATH),
            },
            component="persistence",
        )

    def load_snapshot(self) -> SavedSession | None:
        """Load and deserialize game_state; assemble SavedSession.

        Returns ``None`` when no ``game_state`` row exists for this session.

        Raises ``SaveSchemaIncompatibleError`` on:
          - Corrupt JSON (json.JSONDecodeError)
          - Pydantic validation failure after migration

        Mirrors ``SqliteStore.load`` except:
          - No wal_checkpoint
          - No .canonicalize.bak copy
          - Sessions meta read from the ``sessions`` table (not session_meta)
          - Narrative read inline from ``narrative_log`` (A5 not built yet)
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM game_state WHERE session_id = %s",
                (self._session_id,),
            ).fetchone()

        if row is None:
            _watcher_publish(
                "state_transition",
                {
                    "field": "save",
                    "op": "snapshot_load_empty",
                    "save_path": str(_PG_SAVE_PATH),
                },
                component="persistence",
            )
            return None

        try:
            raw = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise SaveSchemaIncompatibleError(
                save_path=_PG_SAVE_PATH,
                underlying=ValidationError.from_exception_data(
                    title="invalid_save_json", line_errors=[]
                ),
            ) from exc

        migrated = migrate_legacy_snapshot(raw)

        try:
            snapshot = GameSnapshot.model_validate(migrated)
        except ValidationError as exc:
            raise SaveSchemaIncompatibleError(
                save_path=_PG_SAVE_PATH,
                underlying=exc,
            ) from exc

        # Read session metadata from the sessions table (absorbs session_meta).
        # NOTE: _load_meta() and _recent_narrative() below each borrow their own
        # pooled connection, so these reads are NOT in a single MVCC snapshot with
        # the game_state SELECT above. Under a concurrent save_snapshot the
        # snapshot body and sessions.last_played could differ by one turn —
        # accepted as cosmetic on the solo-load / forensic path.
        meta = self._load_meta() or SessionMeta(
            genre_slug=snapshot.genre_slug,
            world_slug=snapshot.world_slug,
            created_at=datetime.now(tz=UTC),
            last_played=datetime.now(tz=UTC),
        )

        entries = self._recent_narrative(3)
        character_names = [ch.core.name for ch in snapshot.characters]
        known_facts = snapshot.characters[0].known_facts if snapshot.characters else []
        recap = _generate_recap(
            entries, character_names, snapshot.party_location() or "", known_facts
        )

        _watcher_publish(
            "state_transition",
            {
                "field": "save",
                "op": "snapshot_loaded",
                "genre_slug": snapshot.genre_slug,
                "world_slug": snapshot.world_slug,
                "round": snapshot.turn_manager.round if snapshot.turn_manager else 0,
                "interaction": snapshot.turn_manager.interaction if snapshot.turn_manager else 0,
                "character_count": len(snapshot.characters),
                "npc_count": len(snapshot.npcs),
                "narrative_entries_in_recap": len(entries),
                "migration_applied": migrated != raw,
                "save_path": str(_PG_SAVE_PATH),
            },
            component="persistence",
        )
        return SavedSession(meta=meta, snapshot=snapshot, recap=recap)

    # ------------------------------------------------------------------
    # world_save
    # ------------------------------------------------------------------

    def load_world_save(self) -> WorldSave:
        """Load hub state or return a fresh default WorldSave.

        Lazy-on-first-read: a session that predates hub-world features
        returns ``WorldSave()`` without writing anything (mirrors
        ``SqliteStore.load_world_save``).

        Raises ``SaveSchemaIncompatibleError`` on JSON or Pydantic failure.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM world_save WHERE session_id = %s",
                (self._session_id,),
            ).fetchone()

        if row is None:
            return WorldSave()

        try:
            raw = json.loads(row[0])
            return WorldSave.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise SaveSchemaIncompatibleError(
                save_path=_PG_SAVE_PATH,
                underlying=(
                    exc
                    if isinstance(exc, ValidationError)
                    else ValidationError.from_exception_data(
                        title="invalid_world_save_json", line_errors=[]
                    )
                ),
            ) from exc

    def save_world_save(self, world_save: WorldSave) -> None:
        """Persist hub state. Atomic via session_tx.

        Mirrors ``SqliteStore.save_world_save``: stamps ``last_saved_at``
        on a copy and upserts into ``world_save`` keyed by ``session_id``.
        """
        now = datetime.now(tz=UTC)
        stamped = world_save.model_copy(update={"last_saved_at": now})
        payload_json = stamped.model_dump_json()

        with session_tx(self._pool, self._session_id) as conn:
            conn.execute(
                """
                INSERT INTO world_save (session_id, payload_json, saved_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE
                    SET payload_json = excluded.payload_json,
                        saved_at     = excluded.saved_at
                """,
                (self._session_id, payload_json, now.isoformat()),
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_meta(self) -> SessionMeta | None:
        """Read session identity from the ``sessions`` table.

        Replaces ``SqliteStore._load_meta`` which reads from ``session_meta``.
        The Postgres ``sessions`` table absorbs both ``session_meta`` and
        ``games`` from the SQLite schema (ADR-115 schema-translation note).
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT genre_slug, world_slug, created_at, last_played "
                "FROM sessions WHERE session_id = %s",
                (self._session_id,),
            ).fetchone()

        if row is None:
            return None
        return SessionMeta(
            genre_slug=row[0],
            world_slug=row[1],
            created_at=_parse_rfc3339(row[2]),
            last_played=_parse_rfc3339(row[3]),
        )

    def _recent_narrative(self, limit: int) -> list[NarrativeEntry]:
        """Return the ``limit`` most-recent narrative entries, oldest-first.

        Inline port of ``SqliteStore.recent_narrative``.  A5 (PgNarrativeStore)
        will provide a proper facade; until then this reads directly from the
        ``narrative_log`` table.

        The ordering sub-query mirrors the SQLite version exactly:
        take the top-N by descending ``id`` (insertion order), then sort
        ascending so the caller sees oldest-to-newest.  The Postgres
        ``id`` column is a BIGINT GENERATED ALWAYS AS IDENTITY so it
        preserves insertion order within a session just as SQLite's
        AUTOINCREMENT does.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT round_number, author, content, tags
                FROM (
                    SELECT id, round_number, author, content, tags
                    FROM narrative_log
                    WHERE session_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                ) sub
                ORDER BY id ASC
                """,
                (self._session_id, limit),
            ).fetchall()

        entries: list[NarrativeEntry] = []
        for row in rows:
            tags_json = row[3] or "[]"
            try:
                tags = json.loads(tags_json)
            except Exception:
                tags = []
            entries.append(
                NarrativeEntry(
                    timestamp=0,
                    round=row[0],
                    author=row[1],
                    content=row[2],
                    tags=tags,
                )
            )
        return entries
