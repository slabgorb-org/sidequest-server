"""Wiring regression: LOCATION_DESCRIPTION must re-emit on slug-resume for
region-mode worlds (sq-playtest 2026-06-02, wry_whimsy/oz).

Per-turn, the region-change branch re-fires LOCATION_DESCRIPTION
(``websocket_session_handler`` region-mode emit, gated on
``_region_changed``), so the Location panel updates as the party moves. But
the connect/resume path emitted *none* of it — ``connect.py`` re-emits
MAP_UPDATE, CONFRONTATION, and CHAPTER_MARKER on resume but never a
LOCATION_DESCRIPTION. Net effect (playtest): reloading a tab mid-region in
``wry_whimsy/oz`` stranded the Location panel on "Gathering your bearings…"
until the next move advanced the region.

``LocationDescriptionMessage``'s own contract docstring says it is "Emitted on
``current_room`` change *and on session resume*" — this test pins the resume
half. Region-mode worlds key the panel off ``current_region`` (the per-turn
region emit passes ``room_id_override=snapshot.current_region``); the resume
bootstrap must do the same so a returning client paints the panel from the
saved region.

Integration-level wiring test (real region-mode content pack + real connect
handler), mirroring ``test_session_handler_slug_resumed`` which uses
caverns/grimvault. ``munchkin_country`` is the world's ``starting_region`` —
the most stable region slug in the pack.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "wry_whimsy"
_WORLD = "oz"
_REGION = "munchkin_country"  # cartography.yaml starting_region
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process-global pool at a per-worker throwaway PG database.

    Mirrors ``test_session_handler_slug_resumed._pg_isolation``: the
    slug-connect path reads the authoritative snapshot from the PG repository,
    not the SQLite save_dir store.
    """
    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _make_handler(save_dir: Path) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-test",
        out_queue=asyncio.Queue(),
    )
    return handler


def _seed_resumable_region_game(slug: str) -> None:
    """Register a resumable SOLO region-mode session with a saved current_region."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    core = CreatureCore(
        name="Susan",
        description="A curious child blown in by a cyclone",
        personality="curious",
        inventory=Inventory(),
    )
    char = Character(
        core=core,
        char_class="Curious Child",
        race="Ordinary-Born",
        backstory="Carried to Oz by a cyclone; wants only to go home.",
    )
    snap = GameSnapshot(
        genre_slug=_GENRE,
        world_slug=_WORLD,
        location="The Munchkin Country",
    )
    snap.characters = [char]
    snap.character_locations["Susan"] = "The Munchkin Country"
    snap.current_region = _REGION
    repo.save(snap)


@pytest.mark.asyncio
async def test_slug_resume_emits_location_description_for_region_mode_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On resume into a region-mode world with a saved ``current_region``, the
    bootstrap must include a LOCATION_DESCRIPTION so the client's Location
    panel paints immediately instead of spinning until the next move.
    """
    # This is a real-content integration test (like ``test_session_handler_
    # slug_resumed``, which loads caverns/grimvault from sidequest-content).
    # ``_maybe_emit_location_description`` re-resolves the world dir through
    # ``DEFAULT_GENRE_PACK_SEARCH_PATHS`` (fresh per-call import), which the
    # server/conftest autouse fixture pins to the frozen fixture packs — and
    # there is no region-mode fixture world. Point it at the real content dir
    # so the helper resolves the same wry_whimsy/oz pack the handler loads.
    import sidequest.genre.loader as _loader_mod

    monkeypatch.setattr(
        _loader_mod,
        "DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [_CONTENT_SEARCH_PATH],
    )

    slug = "2026-06-02-oz-location-resume"
    _seed_resumable_region_game(slug)
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="susan-player",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Susan",
        ),
    )
    outbound = await handler.handle_message(msg)

    loc_msgs = [m for m in outbound if getattr(m, "type", None) == MessageType.LOCATION_DESCRIPTION]
    assert loc_msgs, (
        "Expected LOCATION_DESCRIPTION on slug resume for a region-mode world "
        f"so the Location panel paints from current_region={_REGION!r}. "
        f"Got message types: {[getattr(m, 'type', None) for m in outbound]}"
    )
    assert loc_msgs[0].payload.region_id == _REGION, (
        "Resume LOCATION_DESCRIPTION must key off the saved current_region, "
        f"got region_id={loc_msgs[0].payload.region_id!r}"
    )
