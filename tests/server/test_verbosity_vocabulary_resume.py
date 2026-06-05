"""Slug-resume reports the player's persisted narrator tuning (Story 82-2, AC2).

ADR-049 / context-story-82-2 AC2: "slug-resume populates them (not None)". The
resume ``ready`` event (``handlers/connect.py``) used to hardcode
``narrator_verbosity=None`` / ``narrator_vocabulary=None``, so a returning
player's UI sliders snapped back to defaults instead of their saved choice.

This drives the real slug-resume path against an isolated Postgres (the same
harness as ``test_solo_auto_seat_on_connect.py``): seed a PLAYING snapshot whose
``narrator_verbosity`` / ``narrator_vocabulary`` were persisted, reconnect, and
assert the emitted ``ready`` event carries those values — proving the persisted
choice round-trips the save and reaches the client on resume.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.protocol.enums import NarratorVerbosity, NarratorVocabulary
from sidequest.protocol.messages import SessionEventMessage, SessionEventPayload
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test."""
    import psycopg

    from sidequest.game import db_pool

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
    yield
    db_pool.close_pool()


def _seed_pg_for_slug(slug: str, snap: GameSnapshot, *, mode: GameMode = GameMode.SOLO) -> None:
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(), slug=slug, mode=str(mode), genre_slug=_GENRE, world_slug=_WORLD
    )
    repo.save(snap)


def _make_handler(save_dir: Path) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(
        save_dir=save_dir, genre_pack_search_paths=[_CONTENT_SEARCH_PATH]
    )
    handler.attach_room_context(
        registry=RoomRegistry(), socket_id="sock-test", out_queue=asyncio.Queue()
    )
    return handler


def _seed_playing_snapshot_with_prefs(
    slug: str,
    *,
    verbosity: NarratorVerbosity | None,
    vocabulary: NarratorVocabulary | None,
) -> None:
    """Persist a PLAYING solo snapshot carrying the chosen narrator tuning."""
    core = CreatureCore(
        name="Parsley",
        description="An engineer with the wrong kind of luck",
        personality="trouble-magnet",
        inventory=Inventory(),
    )
    char = Character(core=core, char_class="Engineer", race="Coreworlder", backstory="Arrived")
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, location="Far Landing")
    snap.characters = [char]
    snap.player_seats["parsley-pid"] = "Parsley"
    snap.narrator_verbosity = verbosity
    snap.narrator_vocabulary = vocabulary
    _seed_pg_for_slug(slug, snap, mode=GameMode.SOLO)


def _ready_payload(outbound: list[object]) -> SessionEventPayload:
    """Pull the single SESSION_EVENT{ready} payload out of the connect result."""
    readies = [
        m.payload  # type: ignore[attr-defined]
        for m in outbound
        if isinstance(m, SessionEventMessage) and m.payload.event == "ready"
    ]
    assert readies, (
        f"resume must emit a SESSION_EVENT(ready); got {[type(m).__name__ for m in outbound]}"
    )
    return readies[0]


@pytest.mark.asyncio
async def test_resume_ready_carries_persisted_narrator_settings(tmp_path: Path):
    """A returning PLAYING session whose narrator tuning was persisted must
    report those exact values in the resume ``ready`` event — not None."""
    slug = "2026-06-05-resume-prefs"
    _seed_playing_snapshot_with_prefs(
        slug, verbosity=NarratorVerbosity.concise, vocabulary=NarratorVocabulary.epic
    )

    handler = _make_handler(tmp_path)
    outbound = await handler.handle_message(
        SessionEventMessage(
            type="SESSION_EVENT",
            player_id="parsley-pid",
            payload=SessionEventPayload(event="connect", game_slug=slug, player_name="Parsley"),
        )
    )

    payload = _ready_payload(outbound)
    assert payload.narrator_verbosity == NarratorVerbosity.concise, (
        "resume ready must report the persisted verbosity, not None"
    )
    assert payload.narrator_vocabulary == NarratorVocabulary.epic, (
        "resume ready must report the persisted vocabulary, not None"
    )


@pytest.mark.asyncio
async def test_resume_ready_with_unset_prefs_reports_none(tmp_path: Path):
    """A pre-82-2 save (no persisted choice) resumes with None on both axes —
    the client then shows defaults. Regression guard that the not-None change
    didn't fabricate a value where the player never chose one."""
    slug = "2026-06-05-resume-noprefs"
    _seed_playing_snapshot_with_prefs(slug, verbosity=None, vocabulary=None)

    handler = _make_handler(tmp_path)
    outbound = await handler.handle_message(
        SessionEventMessage(
            type="SESSION_EVENT",
            player_id="parsley-pid",
            payload=SessionEventPayload(event="connect", game_slug=slug, player_name="Parsley"),
        )
    )

    payload = _ready_payload(outbound)
    assert payload.narrator_verbosity is None
    assert payload.narrator_vocabulary is None
