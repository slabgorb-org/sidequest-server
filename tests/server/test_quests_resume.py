"""Wiring regression: QUESTS must hydrate on slug-resume/connect, not wait for
the first narrator turn (sq-playtest 2026-06-23, beneath_sunden MP; 4th instance
of the connect/resume bootstrap re-emit gap — FATE_STATE, LOCATION_DESCRIPTION,
and FATE_DEFEND_REQUEST were the prior three, all fixed in 153-7).

Per-turn, the reactive ``_maybe_emit_quests`` re-projects the quest spine every
turn (same cadence as relationships/fate_state). But the connect/resume bootstrap
in ``sidequest/handlers/connect.py`` re-emits MAP_UPDATE, LOCATION_DESCRIPTION,
CHAPTER_MARKER, PARTY_STATUS, FATE_STATE, and the FATE DEFEND barrier on resume —
but never QUESTS. Net effect (playtest): reloading/reconnecting into a session
with a populated quest spine left the Quests tab blank until the player took an
action, even though ``snapshot.quest_log`` was fully persisted.

The fix mirrors the FATE_STATE resume re-emit (Keith's standing direction: "the
data is in the database, we don't need a narrator loop to hydrate it" — connect
-> read DB -> project). This test pins that: a resumed session with a saved quest
spine emits a QUESTS in the bootstrap, BEFORE any turn.

Integration-level wiring test (real content pack + real connect handler),
mirroring ``test_fate_state_resume`` / ``test_location_description_resume``. The
quest spine is genre-agnostic (ADR-137) and the re-emit lands in the common
bootstrap section, so the world here (wry_whimsy/oz) is just a proven resume
vehicle — what is under test is the quest re-emit, not the world.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    QuestsMessage,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "wry_whimsy"
_WORLD = "oz"
_REGION = "munchkin_country"  # cartography.yaml starting_region
_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

_QUEST_TITLE = "Go Home"
_QUEST_OBJECTIVE = "Return to Kansas"
_ANCHOR = "emerald_city"
_STAKES = "The witch hunts you"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process-global pool at a per-worker throwaway PG database
    (mirrors ``test_fate_state_resume._pg_isolation``)."""
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


def _make_snapshot(*, with_spine: bool) -> GameSnapshot:
    core = CreatureCore(
        name="Groucho",
        description="A skeptic blown in by a cyclone",
        personality="stubborn",
        inventory=Inventory(),
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
    if with_spine:
        snap.quest_log = {
            "q1": QuestEntry(
                title=_QUEST_TITLE,
                objective=_QUEST_OBJECTIVE,
                status="active",
                anchor_id=_ANCHOR,
            )
        }
        snap.quest_anchors = [_ANCHOR]
        snap.active_stakes = _STAKES
    return snap


def _seed_resumable_quest_game(slug: str, *, with_spine: bool) -> None:
    """Register a resumable SOLO session whose persisted snapshot carries a
    populated quest spine (one quest + anchor + stakes) — or, when
    ``with_spine`` is False, the same session with a wholly empty spine."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    repo.save(_make_snapshot(with_spine=with_spine))


def _connect(handler: WebSocketSessionHandler, slug: str):
    return SessionEventMessage(
        type="SESSION_EVENT",
        player_id="groucho-player",
        payload=SessionEventPayload(
            event="connect",
            game_slug=slug,
            player_name="Groucho",
        ),
    )


@pytest.mark.asyncio
async def test_slug_resume_emits_quests_before_first_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On resume into a session with a saved quest spine, the bootstrap must
    include a QUESTS so the Quests tab paints the saved spine immediately,
    instead of staying blank until the first turn.

    RED: fails today — ``connect.py`` never re-emits QUESTS on resume.
    """
    import sidequest.genre.loader as _loader_mod

    monkeypatch.setattr(
        _loader_mod,
        "DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [_CONTENT_SEARCH_PATH],
    )

    slug = "2026-06-23-oz-quest-resume"
    _seed_resumable_quest_game(slug, with_spine=True)
    handler = _make_handler(tmp_path)

    outbound = await handler.handle_message(_connect(handler, slug))

    quest_msgs = [m for m in outbound if isinstance(m, QuestsMessage)]
    assert quest_msgs, (
        "Expected a QUESTS on slug resume so the Quests tab paints the saved "
        "spine immediately (not blank until turn 1). Got message types: "
        f"{[getattr(m, 'type', None) for m in outbound]}"
    )
    assert quest_msgs[0].type == MessageType.QUESTS
    payload = quest_msgs[0].payload
    # The PERSISTED spine — not a default — is what hydrates.
    assert payload.active_stakes == _STAKES, (
        f"Resume QUESTS must carry the saved stakes; got {payload.active_stakes!r}"
    )
    titles = {e.title for e in payload.quest_log}
    assert _QUEST_TITLE in titles, (
        f"Resume QUESTS must carry the persisted quest_log; got titles={titles!r}"
    )
    anchor_ids = {a.anchor_id for a in payload.quest_anchors}
    assert _ANCHOR in anchor_ids, (
        f"Resume QUESTS must carry the persisted anchors; got {anchor_ids!r}"
    )


@pytest.mark.asyncio
async def test_slug_resume_empty_quest_spine_emits_no_quests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard against an UNCONDITIONAL re-emit: a resume into a session with a
    wholly empty quest spine must NOT emit a QUESTS (preserves the wire-parity
    omission contract — empty spine shows nothing, mirrors ``_maybe_emit_quests``
    ``_is_empty_spine`` no-op). The Dev fix must route through the gated emitter,
    not blanket-broadcast an empty payload on every resume.

    Passes today (nothing is emitted) and must STAY green after the fix.
    """
    import sidequest.genre.loader as _loader_mod

    monkeypatch.setattr(
        _loader_mod,
        "DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [_CONTENT_SEARCH_PATH],
    )

    slug = "2026-06-23-oz-quest-resume-empty"
    _seed_resumable_quest_game(slug, with_spine=False)
    handler = _make_handler(tmp_path)

    outbound = await handler.handle_message(_connect(handler, slug))

    quest_msgs = [m for m in outbound if isinstance(m, QuestsMessage)]
    assert quest_msgs == [], (
        "Empty quest spine must emit NO QUESTS on resume (wire-parity omission "
        f"contract). Got {len(quest_msgs)} QUESTS message(s)."
    )
