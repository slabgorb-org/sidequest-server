"""Session persistence value types + helpers (ADR-115 F1).

The legacy SQLite save layer (``SqliteStore`` / ``SqliteSaveRepository`` /
``SAVE_WRITE_LOCK`` / the PRAGMA tuning + ``.canonicalize.bak`` WAL-checkpoint
load path) was retired in ADR-115 TG-F: Postgres is the sole save backend
(``PgSaveRepository`` in ``sidequest/game/pg/``). What survives here are the
storage-engine-agnostic value types still consumed across the codebase:

* ``GameMode`` — solo/multiplayer discriminator.
* ``SaveSchemaIncompatibleError`` — typed error the WebSocket layer catches.
* ``SessionMeta`` / ``SavedSession`` — load() return shapes (PgSnapshot.load).
* ``PersistError`` family — persistence exception hierarchy.
* ``_generate_recap`` — "Previously On…" recap builder (used by PgSnapshot.load).
* ``db_path_for_slug`` — slug→path helper (importer / save_reader / forensics).

Read-only SQLite readers in ``sidequest/game/importer.py`` and
``sidequest/corpus/save_reader.py`` are independent and intentionally untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from sidequest.game.session import GameSnapshot, NarrativeEntry


class SaveSchemaIncompatibleError(Exception):
    """Raised when a saved snapshot fails Pydantic validation against the
    current ``GameSnapshot`` schema.

    The save is not corrupt; it was written by a build whose schema has
    since drifted (e.g. legacy single-``metric`` encounter under the
    dual-dial migration). Callers (session_handler) should catch this
    and surface a typed error frame to the UI rather than letting the
    raw ``ValidationError`` bubble up to the WebSocket layer's broad
    exception handler — which closes the socket without explanation
    and traps the user in an infinite reconnect loop (playtest
    2026-04-25).

    Attributes:
        save_path: Filesystem/sentinel path of the offending save (for the
            user-facing message).
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


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class SessionMeta:
    """Session metadata from the session_meta / sessions table."""

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


# ---------------------------------------------------------------------------
# PersistError hierarchy
# ---------------------------------------------------------------------------


class PersistError(Exception):
    """Errors from persistence operations."""


class NotFoundError(PersistError):
    """Save not found."""


class DatabaseError(PersistError):
    """Database error."""


class SerializationError(PersistError):
    """JSON serialization error."""


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
    """Slug-keyed save-tree path. One directory per game slug.

    Retained post-ADR-115-F1 because the read-only SQLite readers
    (``importer.py``, ``save_reader.py``, forensics tooling) still address
    on-disk saves by this layout, and ``test_legacy_save_endpoints_removed``
    pins it as the canonical helper.
    """
    return save_dir / "games" / slug / "save.db"


def _generate_recap(
    entries: list[NarrativeEntry],
    character_names: list[str],
    location: str,
    known_facts: list,
) -> str | None:
    """Generate a 'Previously On...' recap.

    Uses known_facts as primary source, falls back to narration entries.
    Consumed by ``PgSnapshot.load`` (sidequest/game/pg/snapshot.py).
    """
    if not entries and not known_facts:
        return None

    lines = ["## Previously On…\n"]
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
