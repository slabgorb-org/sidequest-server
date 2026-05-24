"""SQLite session persistence.

GameSnapshot is serialized as JSON TEXT in the ``game_state`` table.

ADR-006 / MP-03: One .db file per game slug (slug-keyed save model).
ADR-023: Auto-save after every turn, atomic writes via SQLite transactions.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from sidequest.game.migrations import migrate_legacy_snapshot
from sidequest.game.session import GameSnapshot, NarrativeEntry
from sidequest.game.world_save import WorldSave
from sidequest.telemetry.spans import SPAN_SESSION_SLOT_REINITIALIZED
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)

# Per-slot tables that ``init_session()`` clears on reinit. ``games``
# (slug-keyed), ``scenario_archive`` (session_id-keyed), and
# ``world_save`` (singleton hub state — Sünden engine plan item 2) are
# all global lifecycle, not per-slot, and survive reinit. ``session_meta``
# is replaced (not cleared) by the INSERT OR REPLACE in ``init_session()``.
#
# Order matters: ``projection_cache`` carries a foreign key to
# ``events.seq`` (PRAGMA foreign_keys=ON in ``_configure_connection``).
# Children must clear before parents.
_PER_SLOT_TABLES: tuple[str, ...] = (
    "projection_cache",
    "events",
    "game_state",
    "narrative_log",
    "scrapbook_entries",
    "lore_fragments",
)


class SaveSchemaIncompatibleError(Exception):
    """Raised by :meth:`SqliteStore.load` when the saved snapshot fails
    Pydantic validation against the current ``GameSnapshot`` schema.

    The save is not corrupt; it was written by a build whose schema has
    since drifted (e.g. legacy single-``metric`` encounter under the
    dual-dial migration). Callers (session_handler) should catch this
    and surface a typed error frame to the UI rather than letting the
    raw ``ValidationError`` bubble up to the WebSocket layer's broad
    exception handler — which closes the socket without explanation
    and traps the user in an infinite reconnect loop (playtest
    2026-04-25).

    Attributes:
        save_path: Filesystem path of the offending save (for the user-
            facing message — they can move it aside manually if needed).
        underlying: The pydantic ValidationError, preserved for logs.
    """

    def __init__(self, save_path: Path, underlying: ValidationError) -> None:
        self.save_path = save_path
        self.underlying = underlying
        super().__init__(
            f"saved snapshot at {save_path} fails current GameSnapshot schema: {underlying}"
        )


# ---------------------------------------------------------------------------
# GameMode
# ---------------------------------------------------------------------------


class GameMode(StrEnum):
    SOLO = "solo"
    MULTIPLAYER = "multiplayer"


@dataclass
class GameRow:
    slug: str
    mode: GameMode
    genre_slug: str
    world_slug: str
    claude_session_id: str | None
    created_at: str


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    genre_slug TEXT NOT NULL,
    world_slug TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_played TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS game_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot_json TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS narrative_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number INTEGER NOT NULL,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_narrative_round ON narrative_log(round_number);
CREATE INDEX IF NOT EXISTS idx_narrative_author ON narrative_log(author);
CREATE TABLE IF NOT EXISTS lore_fragments (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    turn_created INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lore_category ON lore_fragments(category);
CREATE TABLE IF NOT EXISTS scenario_archive (
    session_id TEXT PRIMARY KEY,
    scenario_json TEXT NOT NULL,
    saved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS scrapbook_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL,
    scene_title TEXT,
    scene_type TEXT,
    location TEXT NOT NULL,
    image_url TEXT,
    narrative_excerpt TEXT NOT NULL,
    world_facts TEXT NOT NULL DEFAULT '[]',
    npcs_present TEXT NOT NULL DEFAULT '[]',
    -- Unified render outcome (Story 45-30 + 45-31):
    -- 'rendered' | 'skipped_policy' | 'failed' | 'unavailable'.
    render_status TEXT NOT NULL DEFAULT 'rendered',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scrapbook_turn ON scrapbook_entries(turn_id);
CREATE TABLE IF NOT EXISTS games (
    slug TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('solo', 'multiplayer')),
    genre_slug TEXT NOT NULL,
    world_slug TEXT NOT NULL,
    claude_session_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_seq ON events (seq);
CREATE TABLE IF NOT EXISTS projection_cache (
    event_seq    INTEGER NOT NULL,
    player_id    TEXT NOT NULL,
    include      INTEGER NOT NULL,
    payload_json TEXT,
    PRIMARY KEY (event_seq, player_id),
    FOREIGN KEY (event_seq) REFERENCES events(seq)
);
CREATE INDEX IF NOT EXISTS idx_projection_cache_player ON projection_cache (player_id, event_seq);
CREATE TABLE IF NOT EXISTS world_save (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_telemetry (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq INTEGER,
    round INTEGER,
    ts TEXT NOT NULL,
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_telemetry_round ON turn_telemetry (round);
CREATE INDEX IF NOT EXISTS idx_turn_telemetry_event_seq ON turn_telemetry (event_seq);
CREATE TABLE IF NOT EXISTS location_promotions (
    save_id TEXT NOT NULL,
    region_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    provenance TEXT NOT NULL,
    label TEXT NOT NULL,
    promoted_at_turn INTEGER NOT NULL,
    promoted_canon TEXT NOT NULL,
    new_tier TEXT NOT NULL DEFAULT 'yes_and',
    new_binding_kind TEXT,
    new_binding_ref TEXT,
    PRIMARY KEY (save_id, region_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_location_promotions_region
    ON location_promotions (save_id, region_id);
"""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class SessionMeta:
    """Session metadata from the session_meta table."""

    genre_slug: str
    world_slug: str
    created_at: datetime
    last_played: datetime


@dataclass
class SavedSession:
    """A loaded session: metadata + game state + optional recap."""

    meta: SessionMeta
    snapshot: GameSnapshot
    recap: str | None


@dataclass(frozen=True, slots=True)
class LocationPromotionRow:
    """One mutation to a location's manifest. ADR-109 §4.3 (Story 54-6).

    Two provenances:

    * ``yes_and_promoted`` — authored ``flavor_only`` entity engaged
      mechanically. The row layers a new tier on top of the authored row.
    * ``yes_and_minted`` — player input named an entity not in the
      authored manifest. The row IS the entity.

    Durable per Keith's no-GC retention policy — promotions are never
    reaped on a timer.
    """

    save_id: str
    region_id: str
    entity_id: str
    provenance: str  # 'yes_and_promoted' | 'yes_and_minted'
    label: str
    promoted_at_turn: int
    promoted_canon: str
    new_tier: str  # 'yes_and' in v1
    new_binding_kind: str | None
    new_binding_ref: str | None


# ---------------------------------------------------------------------------
# PersistError
# ---------------------------------------------------------------------------


class PersistError(Exception):
    """Errors from persistence operations."""


class NotFoundError(PersistError):
    """Save not found."""


class DatabaseError(PersistError):
    """SQLite database error."""


class SerializationError(PersistError):
    """JSON serialization error."""


# ---------------------------------------------------------------------------
# PRAGMA Configuration
# ---------------------------------------------------------------------------


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Configure a SQLite connection with standard PRAGMAs.

    Sets WAL journal mode, enables foreign keys, configures row factory,
    and primes a generous busy_timeout so the telemetry sink (which fires
    from any thread that calls ``publish_event``) waits for the writer
    slot instead of failing fast on cross-connection contention.
    (2026-05-18 MP playtest: census/trope_census telemetry dropped every
    turn with ``sqlite3.OperationalError: database is locked``.)
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


# ---------------------------------------------------------------------------
# Process-wide save-DB write lock
# ---------------------------------------------------------------------------

# All writes through any SqliteStore._conn (or any sqlite3.Connection owned
# by SqliteStore) MUST be made inside:
#
#     with SAVE_WRITE_LOCK:
#         with conn:           # SQLite implicit transaction
#             conn.execute(...)
#
# The acquire order is mandatory: SAVE_WRITE_LOCK outside the transaction,
# never the reverse. Acquiring the transaction first and the lock second
# lets two threads both call ``conn.__enter__`` on the shared connection
# (the connection is opened with ``check_same_thread=False``, see
# ``SqliteStore.open``), corrupting the connection's per-statement state
# and producing ``sqlite3.OperationalError: database is locked``.
#
# The lock is reentrant (``threading.RLock``) because the C2 event-append
# transaction in ``sidequest.server.emitters.emit_event`` calls
# ``emit_mechanical_census(...)`` inside its open transaction, which
# publishes watcher events that re-enter ``_persist_turn_telemetry``
# (`sidequest.telemetry.watcher_hub`). Without reentrancy that re-entry
# from the same thread would deadlock.
#
# Consumer modules (kept in sync as writer sites are added):
#   - sidequest.game.persistence
#   - sidequest.server.emitters
#   - sidequest.telemetry.watcher_hub
#
# Future writers landing in any other module must import this lock and
# wrap their writes. The authoritative regression test is
# ``tests/server/test_save_write_lock.py`` — anyone adding a 15th write
# site without acquiring the lock will see it fail under concurrent load.
SAVE_WRITE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# SqliteStore
# ---------------------------------------------------------------------------


class SqliteStore:
    """SQLite-backed session store. One .db file per save slot.

    Uses singleton tables (session_meta, game_state) plus append-only
    narrative_log. Built on stdlib sqlite3.

    All writes through ``self._conn`` must hold ``SAVE_WRITE_LOCK`` from
    this module — see the lock's module-level doc block above.
    """

    def __init__(self, conn: sqlite3.Connection | Path) -> None:
        if isinstance(conn, Path):
            c = sqlite3.connect(str(conn), check_same_thread=False)
            _configure_connection(c)
            self._conn = c
            self._path: Path | None = conn
        else:
            self._conn = conn
            self._path = None
        self._init_schema()

    @classmethod
    def open_in_memory(cls) -> SqliteStore:
        """Open an in-memory store (for testing)."""
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return cls(conn)

    @classmethod
    def open(cls, path: str) -> SqliteStore:
        """Open a file-backed store.

        ``check_same_thread=False`` is intentional: the watcher hub's
        telemetry sink (``_persist_turn_telemetry``) is invoked from any
        thread that calls ``publish_event`` — narrator workers, the
        renderer, the daemon client. Per-write serialization is enforced
        at the watcher layer with a module-level lock; without that lock
        this flag would not be safe.
        """
        conn = sqlite3.connect(path, check_same_thread=False)
        _configure_connection(conn)
        store = cls(conn)
        store._path = Path(path)
        return store

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        self._apply_migrations()
        self._conn.commit()

    def _apply_migrations(self) -> None:
        """Idempotent column adds for tables that pre-existed before a
        new field was introduced. ``CREATE TABLE IF NOT EXISTS`` is
        a no-op on an existing table, so older DBs miss columns added
        in the schema literal. SQLite has no ``ADD COLUMN IF NOT EXISTS``
        prior to 3.35, but ``ALTER TABLE ... ADD COLUMN`` raises a
        catchable ``OperationalError`` on a duplicate column — we
        treat that as the success case.
        """
        # Story 45-31: scrapbook_entries.render_status — degradation
        # marker for the unavailable-fallback path. Older DBs created
        # before this column existed need it added.
        try:
            self._conn.execute("ALTER TABLE scrapbook_entries ADD COLUMN render_status TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    def initialize(self) -> None:
        """Public alias for _init_schema — re-runs schema creation (idempotent)."""
        self._init_schema()

    def init_session(self, genre_slug: str, world_slug: str) -> None:
        """Initialize or reinitialize a save slot.

        Atomically clears every per-slot table (``_PER_SLOT_TABLES``) and
        replaces ``session_meta`` row 1 with the new genre/world identity.
        Either the whole transaction commits or none of it does — there is
        no half-clear state. The slug-keyed ``games`` table and the global
        ``scenario_archive`` are out of scope for per-slot lifecycle and
        are preserved across reinits.

        Emits a ``session.slot_reinitialized`` watcher event on every call,
        including against a fresh slot, so the GM panel sees the negative
        confirmation that reinit ran (zero priors) as well as the positive
        one (non-zero priors).
        """
        prior_narrative_count = self._conn.execute("SELECT COUNT(*) FROM narrative_log").fetchone()[
            0
        ]
        prior_event_count = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        with self._conn:
            for tbl in _PER_SLOT_TABLES:
                self._conn.execute(f"DELETE FROM {tbl}")
            now = _now_rfc3339()
            self._conn.execute(
                """INSERT OR REPLACE INTO session_meta
                   (id, genre_slug, world_slug, created_at, last_played, schema_version)
                   VALUES (1, ?, ?, ?, ?, 1)""",
                (genre_slug, world_slug, now, now),
            )

        _watcher_publish(
            SPAN_SESSION_SLOT_REINITIALIZED,
            {
                "genre_slug": genre_slug,
                "world_slug": world_slug,
                "cleared_tables": list(_PER_SLOT_TABLES),
                "prior_narrative_count": int(prior_narrative_count),
                "prior_event_count": int(prior_event_count),
                "mode": "clear",
            },
            component="session",
        )

    def save(self, snapshot: GameSnapshot) -> None:
        """Save the current game state.

        Serializes GameSnapshot to JSON and stores in game_state table.
        Updates last_played in session_meta. Atomic via transaction.

        Emits a watcher event on success (sprint 3 cold-subsystem audit)
        so the GM panel can prove the save actually committed every
        turn — not just "we got to the end of the apply pipeline."
        """
        now = datetime.now(tz=UTC)
        snapshot_copy = snapshot.model_copy(update={"last_saved_at": now})
        state_json = snapshot_copy.model_dump_json()
        now_str = now.isoformat()

        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO game_state (id, snapshot_json, saved_at)
                   VALUES (1, ?, ?)""",
                (state_json, now_str),
            )
            self._conn.execute(
                "UPDATE session_meta SET last_played = ? WHERE id = 1",
                (now_str,),
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
                "save_path": str(self._path) if self._path else "<in-memory>",
            },
            component="persistence",
        )

    def load(self) -> SavedSession | None:
        """Load the saved session, or None if no save exists.

        Raises ``SaveSchemaIncompatibleError`` when the snapshot JSON
        fails Pydantic validation against the current ``GameSnapshot``
        schema. Callers must catch this and surface a typed error frame
        to the UI rather than letting the raw ValidationError bubble up
        to the WebSocket layer (which closes the socket without
        explanation, trapping the user in an infinite reconnect loop).
        """
        row = self._conn.execute("SELECT snapshot_json FROM game_state WHERE id = 1").fetchone()
        if row is None:
            _watcher_publish(
                "state_transition",
                {
                    "field": "save",
                    "op": "snapshot_load_empty",
                    "save_path": str(self._path) if self._path else "<in-memory>",
                },
                component="persistence",
            )
            return None

        try:
            raw = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise SaveSchemaIncompatibleError(
                save_path=self._path or Path("<in-memory>"),
                underlying=ValidationError.from_exception_data(
                    title="invalid_save_json", line_errors=[]
                ),
            ) from exc
        migrated = migrate_legacy_snapshot(raw)

        # Architect amendment 2026-05-04: sibling-file safety net.
        # If migration rewrote anything and we have a real on-disk save,
        # copy the .db to <save>.db.canonicalize.bak ONCE. The .bak is
        # never reaped — durable retention per Keith's playstyle.
        #
        # WAL note: PRAGMA journal_mode=WAL means uncheckpointed writes
        # live in <save>.db-wal until a checkpoint runs. A naked
        # ``shutil.copy2`` of the .db alone copies a file that may be
        # missing the most recent rows. We force a TRUNCATE checkpoint
        # before the copy so the .bak is a single self-contained file
        # (matching how Keith would expect to recover from one — no
        # WAL/SHM siblings to keep track of).
        if migrated != raw and self._path is not None:
            bak_path = self._path.with_suffix(self._path.suffix + ".canonicalize.bak")
            if not bak_path.exists():
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    shutil.copy2(self._path, bak_path)
                except (OSError, sqlite3.Error) as bak_exc:
                    # Defense-in-depth, not primary gate: don't block load.
                    logger.warning(
                        "snapshot.canonicalize backup failed for %s: %s",
                        self._path,
                        bak_exc,
                    )

        try:
            snapshot = GameSnapshot.model_validate(migrated)
        except ValidationError as exc:
            raise SaveSchemaIncompatibleError(
                save_path=self._path or Path("<in-memory>"),
                underlying=exc,
            ) from exc
        meta = self._load_meta() or SessionMeta(
            genre_slug=snapshot.genre_slug,
            world_slug=snapshot.world_slug,
            created_at=datetime.now(tz=UTC),
            last_played=datetime.now(tz=UTC),
        )
        entries = self.recent_narrative(3)
        character_names = [ch.core.name for ch in snapshot.characters]
        known_facts = snapshot.characters[0].known_facts if snapshot.characters else []
        # Wave 2B (story 45-48): use the consensus party location for the
        # recap header — solo always returns the only PC's location;
        # MP returns the shared location when seated PCs agree, "" when split.
        recap = _generate_recap(
            entries, character_names, snapshot.party_location() or "", known_facts
        )
        # Sprint 3 cold-subsystem audit: resume reconstruction was nearly
        # invisible to the GM panel — only the slot reinit span fired,
        # which doesn't say anything about the actual snapshot integrity.
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
                "save_path": str(self._path) if self._path else "<in-memory>",
            },
            component="persistence",
        )
        return SavedSession(meta=meta, snapshot=snapshot, recap=recap)

    def load_world_save(self) -> WorldSave:
        """Load this campaign's hub state, or a fresh empty WorldSave.

        Lazy-on-first-read: a save that predates this feature, or a new
        save in a non-hub world, returns a default-populated WorldSave
        without writing anything to disk. The first write happens via
        save_world_save() (called from item 4's recruit / delve-end flows).

        Raises SaveSchemaIncompatibleError on JSON or pydantic validation
        failure — same pattern as load(), so the websocket layer's typed
        error path catches it cleanly.
        """
        row = self._conn.execute("SELECT payload_json FROM world_save WHERE id = 1").fetchone()
        if row is None:
            return WorldSave()
        try:
            raw = json.loads(row[0])
            return WorldSave.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise SaveSchemaIncompatibleError(
                save_path=self._path or Path("<in-memory>"),
                underlying=(
                    exc
                    if isinstance(exc, ValidationError)
                    else ValidationError.from_exception_data(
                        title="invalid_world_save_json", line_errors=[]
                    )
                ),
            ) from exc

    def save_world_save(self, world_save: WorldSave) -> None:
        """Persist this campaign's hub state. Atomic via transaction."""
        now = datetime.now(tz=UTC)
        stamped = world_save.model_copy(update={"last_saved_at": now})
        payload_json = stamped.model_dump_json()
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO world_save (id, payload_json, saved_at)
                   VALUES (1, ?, ?)""",
                (payload_json, now.isoformat()),
            )

    def append_narrative(self, entry: NarrativeEntry) -> None:
        """Append a narrative entry to the log."""
        import json

        tags_json = json.dumps(entry.tags)
        self._conn.execute(
            """INSERT INTO narrative_log (round_number, author, content, tags)
               VALUES (?, ?, ?, ?)""",
            (entry.round, entry.author, entry.content, tags_json),
        )
        self._conn.commit()

    def max_narrative_round(self) -> int:
        """Return ``MAX(round_number)`` from ``narrative_log``, or 0 when empty.

        Story 45-11 AC4: powers the ``turn_manager.round_invariant`` span
        emitted at the end of every narration tick. Returns 0 (not None,
        does not raise) on an empty log so the GM-panel chart axis is
        always plottable on the first tick of a new session.
        """
        row = self._conn.execute("SELECT MAX(round_number) FROM narrative_log").fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    def recent_narrative(self, limit: int) -> list[NarrativeEntry]:
        """Get the most recent narrative entries, ordered oldest-first."""
        import json

        rows = self._conn.execute(
            """SELECT round_number, author, content, tags
               FROM (SELECT * FROM narrative_log ORDER BY id DESC LIMIT ?)
               ORDER BY id ASC""",
            (limit,),
        ).fetchall()
        entries = []
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

    def generate_recap(self) -> str | None:
        """Generate a 'Previously On...' recap from recent entries."""
        entries = self.recent_narrative(3)
        if not entries:
            return None
        recap = "## Previously On\u2026\n\n"
        for entry in entries:
            content = entry.content
            if len(content) > 200:
                content = content[:200] + "..."
            recap += f"- {content}\n"
        return recap

    def list_location_promotions(
        self, *, save_id: str, region_id: str
    ) -> list[LocationPromotionRow]:
        """Return all location_promotions rows for ``(save_id, region_id)``,
        ordered by ``promoted_at_turn`` then ``entity_id``. ADR-109 §4.3."""
        rows = self._conn.execute(
            """SELECT save_id, region_id, entity_id, provenance, label,
                      promoted_at_turn, promoted_canon, new_tier,
                      new_binding_kind, new_binding_ref
                 FROM location_promotions
                WHERE save_id = ? AND region_id = ?
                ORDER BY promoted_at_turn ASC, entity_id ASC""",
            (save_id, region_id),
        ).fetchall()
        return [
            LocationPromotionRow(
                save_id=row[0],
                region_id=row[1],
                entity_id=row[2],
                provenance=row[3],
                label=row[4],
                promoted_at_turn=row[5],
                promoted_canon=row[6],
                new_tier=row[7],
                new_binding_kind=row[8],
                new_binding_ref=row[9],
            )
            for row in rows
        ]

    def upsert_location_promotion(self, row: LocationPromotionRow) -> None:
        """Insert or update one ``location_promotions`` row.

        Primary key ``(save_id, region_id, entity_id)``. Re-engagement of
        the same entity updates ``promoted_at_turn`` / ``promoted_canon``
        / ``new_tier`` / binding fields in place rather than minting a
        duplicate row (ADR-109 §4.3, AC-3 in story 54-6).
        """
        with self._conn:
            self._conn.execute(
                """INSERT INTO location_promotions (
                       save_id, region_id, entity_id, provenance, label,
                       promoted_at_turn, promoted_canon, new_tier,
                       new_binding_kind, new_binding_ref
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(save_id, region_id, entity_id) DO UPDATE SET
                       provenance = excluded.provenance,
                       label = excluded.label,
                       promoted_at_turn = excluded.promoted_at_turn,
                       promoted_canon = excluded.promoted_canon,
                       new_tier = excluded.new_tier,
                       new_binding_kind = excluded.new_binding_kind,
                       new_binding_ref = excluded.new_binding_ref""",
                (
                    row.save_id,
                    row.region_id,
                    row.entity_id,
                    row.provenance,
                    row.label,
                    row.promoted_at_turn,
                    row.promoted_canon,
                    row.new_tier,
                    row.new_binding_kind,
                    row.new_binding_ref,
                ),
            )

    def _load_meta(self) -> SessionMeta | None:
        row = self._conn.execute(
            """SELECT genre_slug, world_slug, created_at, last_played
               FROM session_meta WHERE id = 1"""
        ).fetchone()
        if row is None:
            return None
        return SessionMeta(
            genre_slug=row[0],
            world_slug=row[1],
            created_at=_parse_rfc3339(row[2]),
            last_played=_parse_rfc3339(row[3]),
        )

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def connection(self) -> sqlite3.Connection:
        """Return the live sqlite3 connection.

        Plan 7 (spec §7.5): DungeonStore wraps this exact connection so
        game-save + dungeon-save share one transaction. Never hands out a
        copy — callers must observe each other's writes.
        """
        return self._conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_rfc3339() -> str:
    return datetime.now(tz=UTC).isoformat()


def _parse_rfc3339(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(tz=UTC)


def db_path_for_slug(save_dir: Path, slug: str) -> Path:
    """New slug-keyed DB path. One .db per game slug."""
    return save_dir / "games" / slug / "save.db"


def upsert_game(
    store: SqliteStore,
    *,
    slug: str,
    mode: GameMode,
    genre_slug: str,
    world_slug: str,
) -> None:
    """Insert a game row if the slug is new; no-op if it exists.

    All creation-time fields (mode, genre_slug, world_slug) are frozen — a
    subsequent call with different values is intentionally ignored via
    ``ON CONFLICT(slug) DO NOTHING``. Same-day, same-world collisions are the
    resume path by design; the caller can re-invoke without branching on
    "already exists?".
    """
    with store._conn:
        store._conn.execute(
            """INSERT INTO games (slug, mode, genre_slug, world_slug, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(slug) DO NOTHING""",
            (slug, mode.value, genre_slug, world_slug, _now_rfc3339()),
        )


def get_game(store: SqliteStore, slug: str) -> GameRow | None:
    row = store._conn.execute(
        "SELECT slug, mode, genre_slug, world_slug, claude_session_id, created_at FROM games WHERE slug = ?",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    return GameRow(
        slug=row[0],
        mode=GameMode(row[1]),
        genre_slug=row[2],
        world_slug=row[3],
        claude_session_id=row[4],
        created_at=row[5],
    )


def set_claude_session_id(store: SqliteStore, slug: str, claude_session_id: str) -> None:
    with store._conn:
        store._conn.execute(
            "UPDATE games SET claude_session_id = ? WHERE slug = ?",
            (claude_session_id, slug),
        )


def query_encounter_events(store: SqliteStore) -> list[dict]:
    """Return ordered ENCOUNTER_* event rows as dicts.

    The GM panel reads this for its post-hoc timeline view (spec
    2026-04-25-dual-track-momentum-design.md §"GM panel verification").
    """
    import json

    rows = store._conn.execute(
        "SELECT seq, kind, payload_json, created_at FROM events "
        "WHERE kind LIKE 'ENCOUNTER_%' ORDER BY seq"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "seq": r[0],
                "kind": r[1],
                "payload": json.loads(r[2]),
                "created_at": r[3],
            }
        )
    return out


def _generate_recap(
    entries: list[NarrativeEntry],
    character_names: list[str],
    location: str,
    known_facts: list,
) -> str | None:
    """Generate a 'Previously On...' recap.

    Uses known_facts as primary source, falls back to narration entries.
    """
    if not entries and not known_facts:
        return None

    lines = ["## Previously On\u2026\n"]
    if character_names:
        party = ", ".join(character_names)
        lines.append(f"The party — {party} — had been adventuring.\n")

    if known_facts:
        # Use up to 8 most recent known facts
        for fact in known_facts[-8:]:
            content = getattr(fact, "content", None) or (
                fact.get("content", "") if isinstance(fact, dict) else ""
            )
            if content:
                lines.append(f"- {content}")
    elif entries:
        for entry in entries:
            content = entry.content
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"- {content}")

    if location:
        lines.append(f"\nThe party now finds themselves at {location}.")

    return "\n".join(lines)
