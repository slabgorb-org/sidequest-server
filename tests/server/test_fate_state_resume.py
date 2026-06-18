"""Wiring regression: FATE_STATE must hydrate on slug-resume/connect, not wait
for the first narrator turn (sq-playtest 2026-06-17, wry_whimsy/oz).

Per-turn, ``_execute_narration_turn`` re-projects FATE_STATE every turn (the
reactive emitter rides the same cadence as relationships/quests). But the
connect/resume bootstrap emitted *none* of it — ``connect.py`` re-emits
MAP_UPDATE, LOCATION_DESCRIPTION, CHAPTER_MARKER, PARTY_STATUS, and
CONFRONTATION on resume, but never FATE_STATE. Net effect (playtest, DRIVER
session 2026-06-17-oz-f9d7524d): reloading a Fate session left the Character →
Stats panel showing "No stats available" until the player took an action — the
fully-populated ``core.fate_sheet`` was invisible until turn 1.

Keith's direction (load-bearing): "the data is in the database, we don't need a
narrator loop to hydrate it" — emit FATE_STATE directly from the persisted
sheet at connect/bootstrap. Connect -> read DB -> project. This test pins that:
a resumed Fate session emits a FATE_STATE in the bootstrap, BEFORE any turn.

Integration-level wiring test (real Fate content pack + real connect handler),
mirroring ``test_location_description_resume`` — same wry_whimsy/oz world,
same resume harness, plus a populated Fate sheet on the resumed PC.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    FateStateMessage,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "wry_whimsy"
_WORLD = "oz"
_REGION = "munchkin_country"  # cartography.yaml starting_region
_HIGH_CONCEPT = "Stubborn Skeptic Who Argues With Doorknobs"
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process-global pool at a per-worker throwaway PG database
    (mirrors ``test_location_description_resume._pg_isolation``)."""
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


def _seed_resumable_fate_game(slug: str) -> None:
    """Register a resumable SOLO Fate session whose PC carries a player-authored
    Fate sheet (high concept + a ladder skill + fate points)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    sheet = FateSheet(skills={"Provoke": 4, "Will": 3}, fate_points=3, refresh=3)
    sheet.aspects.append(Aspect(text=_HIGH_CONCEPT, kind="high_concept"))
    sheet.aspects.append(Aspect(text="Curiosity Always Gets the Better of Me", kind="trouble"))
    core = CreatureCore(
        name="Groucho",
        description="A skeptic blown in by a cyclone",
        personality="stubborn",
        inventory=Inventory(),
        fate_sheet=sheet,
    )
    char = Character(
        core=core,
        char_class="Stubborn Skeptic",
        race="Ordinary-Born",
        backstory="Argued his way into Oz and intends to argue his way out.",
    )
    snap = GameSnapshot(
        genre_slug=_GENRE,
        world_slug=_WORLD,
        location="The Munchkin Country",
    )
    snap.characters = [char]
    snap.character_locations["Groucho"] = "The Munchkin Country"
    snap.current_region = _REGION
    repo.save(snap)


@pytest.mark.asyncio
async def test_slug_resume_emits_fate_state_before_first_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On resume into a Fate world with a saved fate_sheet, the bootstrap must
    include a FATE_STATE so the Character → Stats panel paints the sheet
    immediately instead of showing "No stats available" until the first turn.
    """
    import sidequest.genre.loader as _loader_mod

    monkeypatch.setattr(
        _loader_mod,
        "DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [_CONTENT_SEARCH_PATH],
    )

    slug = "2026-06-17-oz-fate-resume"
    _seed_resumable_fate_game(slug)
    handler = _make_handler(tmp_path)

    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="groucho-player",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Groucho",
        ),
    )
    outbound = await handler.handle_message(msg)

    fate_msgs = [m for m in outbound if isinstance(m, FateStateMessage)]
    assert fate_msgs, (
        "Expected a FATE_STATE on slug resume for a Fate world so the Character "
        "panel paints the saved sheet immediately (not 'No stats available' until "
        f"turn 1). Got message types: {[getattr(m, 'type', None) for m in outbound]}"
    )
    assert fate_msgs[0].type == MessageType.FATE_STATE
    entry = fate_msgs[0].payload.characters[0]
    assert entry.name == "Groucho"
    # The persisted, player-authored sheet — not a default — is what hydrates.
    aspect_texts = [a.text for a in entry.aspects]
    assert _HIGH_CONCEPT in aspect_texts, (
        f"Resume FATE_STATE must carry the PERSISTED sheet's aspects; got aspects={aspect_texts!r}"
    )
    skill_names = {s.name for s in entry.skills}
    assert {"Provoke", "Will"} <= skill_names, (
        f"Resume FATE_STATE must carry the saved ladder skills; got {skill_names!r}"
    )
