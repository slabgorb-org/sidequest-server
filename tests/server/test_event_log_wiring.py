"""NARRATION seq wiring + EventLog persistence (via direct handler dispatch).

Two assertions, both inside SessionHandler: NARRATION payloads carry a
monotonic ``seq`` field sourced from EventLog, and EventLog gains a
NARRATION row per turn. No FastAPI, no TestClient, no websocket hop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.sqlite_repository import SqliteSaveRepository
from sidequest.protocol import GameMessage
from sidequest.protocol.enums import MessageType
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
_SLUG = "event-log-wiring-fixture"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


def _seed_with_character(tmp_path: Path, slug: str) -> None:
    """Seed a SOLO game row + a saved snapshot carrying one Character, so
    the slug-connect branch goes straight to Playing (skipping chargen)."""
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.SOLO,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    core = CreatureCore(
        name="Thorn",
        description="A wandering fighter",
        personality="Grim",
        inventory=Inventory(),
    )
    char = Character(
        core=core,
        char_class="Fighter",
        race="Human",
        backstory="A wanderer.",
    )
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [char]
    store.init_session(_GENRE, _WORLD)
    store.save(snap)
    store.close()


def _fake_narration_result():
    from sidequest.agents.orchestrator import NarrationTurnResult

    return NarrationTurnResult(
        narration="The dungeon echoes with your footsteps.",
        location=None,
        quest_updates={},
        lore_established=[],
        npcs_present=[],
        is_degraded=False,
        agent_duration_ms=42,
    )


@pytest.mark.asyncio
async def test_narration_carries_seq_and_event_log_has_row(tmp_path: Path) -> None:
    _seed_with_character(tmp_path, _SLUG)
    registry = RoomRegistry()
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=registry,
        socket_id="sock-alice",
        out_queue=queue,
    )

    connect = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "alice",
            "payload": {
                "event": "connect",
                "game_slug": _SLUG,
                "last_seen_seq": 0,
            },
        }
    )
    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration_result()),
    ):
        connect_out = await handler.handle_message(connect)

        # Sanity: connected event must advertise has_character=True so the
        # session is in Playing state for the PLAYER_ACTION below.
        connected = [
            m for m in connect_out if getattr(m, "type", None) == MessageType.SESSION_EVENT
        ]
        assert connected, f"expected SESSION_EVENT connected; got {connect_out}"
        assert getattr(connected[0].payload, "has_character", False) is True

        action = GameMessage.model_validate(
            {
                "type": "PLAYER_ACTION",
                "player_id": "alice",
                "payload": {"action": "I look around the dungeon."},
            }
        )
        action_out = await handler.handle_message(action)

    narrations = [m for m in action_out if getattr(m, "type", None) == MessageType.NARRATION]
    assert narrations, f"PLAYER_ACTION must produce a NARRATION frame; got {action_out}"
    seq = getattr(narrations[0].payload, "seq", None)
    assert seq is not None, f"NARRATION payload missing seq: {narrations[0].payload}"
    assert seq >= 1, f"expected seq >= 1, got {seq}"

    # Verify EventLog persisted the row.
    db = db_path_for_slug(tmp_path, _SLUG)
    store = SqliteStore(db)
    store.initialize()
    try:
        rows = EventLog(SqliteSaveRepository(store)).read_since(since_seq=0)
        narration_rows = [r for r in rows if r.kind == "NARRATION"]
        assert narration_rows, f"expected at least one NARRATION row in EventLog; got {rows}"
    finally:
        store.close()
