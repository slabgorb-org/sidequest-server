"""Integration: late joiner catches up via event replay (MP-03 Task 9).

Verifies end-to-end sync wiring:
1. Alice connects to a multiplayer game, seats, and sends PLAYER_ACTION.
2. Alice receives NARRATION with seq >= 1, which is logged in EventLog.
3. Bob connects to the same game with last_seen_seq=0.
4. Bob receives the replay of Alice's NARRATION at the same seq value.

Uses caverns_and_claudes / grimvault (genre/world available in the content repo).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from sidequest.game.persistence import GameMode, SqliteStore, db_path_for_slug, upsert_game
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS
from sidequest.server.app import create_app

_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"
_SLUG = "2026-04-22-grimvault-sync-wiring"


def _seed(tmp_path: Path, slug: str) -> None:
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.MULTIPLAYER,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    store.close()


def _genre_packs_path() -> Path | None:
    return next(
        (p for p in DEFAULT_GENRE_PACK_SEARCH_PATHS if p.exists()),
        None,
    )


def _make_fake_narration_result() -> object:
    """Minimal NarrationTurnResult-like object for mocking the orchestrator."""
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


def test_late_joiner_catches_up(tmp_path: Path) -> None:
    """Alice sends NARRATION, bob connects with last_seen_seq=0 and receives replay."""
    packs = _genre_packs_path()
    if packs is None:
        pytest.skip(f"No genre_packs directory found in {DEFAULT_GENRE_PACK_SEARCH_PATHS}")

    _seed(tmp_path, _SLUG)
    app = create_app(genre_pack_search_paths=[packs], save_dir=tmp_path)
    client = TestClient(app)

    # Re-seed with a character so we skip chargen
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.session import GameSnapshot

    db = db_path_for_slug(tmp_path, _SLUG)
    store = SqliteStore(db)
    store.initialize()
    # Build minimal character to mark has_character=True
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

    fake_result = _make_fake_narration_result()

    with (
        patch(
            "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
            new=AsyncMock(return_value=fake_result),
        ),
        client.websocket_connect("/ws") as ws_alice,
    ):
        # Alice connects and seats
        ws_alice.send_json(
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
        alice_connected = ws_alice.receive_json()
        assert alice_connected["type"] == "SESSION_EVENT"
        assert alice_connected["payload"]["event"] == "connected"
        # Drain the resume bootstrap messages (has_character=True path):
        # SESSION_EVENT{ready} + PARTY_STATUS.
        ready_msg = ws_alice.receive_json()
        assert ready_msg["type"] == "SESSION_EVENT"
        assert ready_msg["payload"]["event"] == "ready"
        party_status_msg = ws_alice.receive_json()
        assert party_status_msg["type"] == "PARTY_STATUS"

        # Alice claims a seat
        ws_alice.send_json(
            {
                "type": "PLAYER_SEAT",
                "player_id": "alice",
                "payload": {"character_slot": "rux"},
            }
        )
        alice_seat_confirmed = ws_alice.receive_json()
        assert alice_seat_confirmed["type"] == "SEAT_CONFIRMED"

        # Alice sends a PLAYER_ACTION
        ws_alice.send_json(
            {
                "type": "PLAYER_ACTION",
                "player_id": "alice",
                "payload": {"action": "I look around the grimvault."},
            }
        )

        # Alice receives NARRATION with seq >= 1
        alice_narration = None
        for _ in range(10):
            m = ws_alice.receive_json()
            if m["type"] == "NARRATION":
                alice_narration = m
                break

        assert alice_narration is not None, "Expected NARRATION from Alice's action"
        assert "seq" in alice_narration["payload"]
        first_seq = alice_narration["payload"]["seq"]
        assert first_seq >= 1, f"Expected seq >= 1, got {first_seq}"

        # Late joiner Bob connects with last_seen_seq=0 to the same slug
        with client.websocket_connect("/ws") as ws_bob:
            ws_bob.send_json(
                {
                    "type": "SESSION_EVENT",
                    "player_id": "bob",
                    "payload": {
                        "event": "connect",
                        "game_slug": _SLUG,
                        "last_seen_seq": 0,
                    },
                }
            )
            bob_connected = ws_bob.receive_json()
            assert bob_connected["type"] == "SESSION_EVENT"
            assert bob_connected["payload"]["event"] == "connected"

            # Bob may receive PLAYER_PRESENCE from alice as a side effect.
            # Drain up to 5 messages looking for the replay NARRATION.
            replay_narration = None
            for _ in range(5):
                m = ws_bob.receive_json()
                if m["type"] == "NARRATION":
                    replay_narration = m
                    break

            assert replay_narration is not None, (
                "Expected NARRATION replay for bob after connecting with last_seen_seq=0"
            )
            assert replay_narration["payload"]["seq"] == first_seq, (
                f"Expected replay seq to match alice's seq ({first_seq}), "
                f"got {replay_narration['payload']['seq']}"
            )
