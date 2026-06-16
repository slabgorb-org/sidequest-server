"""Integration: narrator pauses when a seated player disconnects (MP-02 Task 6).

Verifies:
1. When all seated players are present, PLAYER_ACTION dispatches to the narrator.
2. When a seated player disconnects, PLAYER_PRESENCE{disconnected} is broadcast
   followed by GAME_PAUSED with the absent player listed in waiting_for.
3. A PLAYER_ACTION sent while paused returns GAME_PAUSED (not narration) and does
   NOT call the narrator dispatch method.
4. When the absent player reconnects, GAME_RESUMED is broadcast to the room.

Uses caverns_and_claudes / grimvault (genre/world available in the content repo).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sidequest.game.persistence import GameMode, SqliteStore, db_path_for_slug, upsert_game
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS
from sidequest.server.app import create_app

_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"
_SLUG_BASE = "2026-04-22-grimvault-pause"


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


# ---------------------------------------------------------------------------
# Test 1: PLAYER_ACTION while paused returns GAME_PAUSED, skips narrator
# ---------------------------------------------------------------------------


def test_player_action_while_paused_returns_game_paused_not_narration(
    tmp_path: Path,
) -> None:
    """Alice and Bob both seat. Bob disconnects. Alice sends PLAYER_ACTION.
    Expects GAME_PAUSED back, NOT a NARRATION; narrator dispatch is NOT called.
    """
    packs = _genre_packs_path()
    if packs is None:
        pytest.skip(f"No genre_packs directory found in {DEFAULT_GENRE_PACK_SEARCH_PATHS}")

    slug = _SLUG_BASE + "-action-gate"
    _seed(tmp_path, slug)
    app = create_app(genre_pack_search_paths=[packs], save_dir=tmp_path)
    client = TestClient(app)

    narrator_calls: list[Any] = []

    from sidequest.server.session_handler import WebSocketSessionHandler

    original_execute = WebSocketSessionHandler._execute_narration_turn

    async def _recording_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        narrator_calls.append(args)
        return await original_execute(self, *args, **kwargs)

    with (
        patch.object(
            WebSocketSessionHandler,
            "_execute_narration_turn",
            new=_recording_execute,
        ),
        client.websocket_connect("/ws") as ws_alice,
    ):
        # Alice connects
        ws_alice.send_json(
            {
                "type": "SESSION_EVENT",
                "player_id": "alice",
                "payload": {"event": "connect", "game_slug": slug},
            }
        )
        msg = ws_alice.receive_json()
        assert msg["type"] == "SESSION_EVENT"
        assert msg["payload"]["event"] == "connected"
        # Drain chargen bootstrap (slug path emits it when has_character=False).
        alice_chargen = ws_alice.receive_json()
        assert alice_chargen["type"] == "CHARACTER_CREATION"

        # Alice claims a seat
        ws_alice.send_json(
            {
                "type": "PLAYER_SEAT",
                "player_id": "alice",
                "payload": {"character_slot": "rux"},
            }
        )
        seat_msg = ws_alice.receive_json()
        assert seat_msg["type"] == "SEAT_CONFIRMED"

        # Bob connects to the same slug then immediately disconnects
        with client.websocket_connect("/ws") as ws_bob:
            ws_bob.send_json(
                {
                    "type": "SESSION_EVENT",
                    "player_id": "bob",
                    "payload": {"event": "connect", "game_slug": slug},
                }
            )
            bob_connected = ws_bob.receive_json()
            # Drain bob's chargen bootstrap.
            bob_chargen = ws_bob.receive_json()
            assert bob_chargen["type"] == "CHARACTER_CREATION"
            # Alice should receive a PLAYER_PRESENCE{connected} for bob
            alice_sees_bob = ws_alice.receive_json()
            assert alice_sees_bob["type"] == "PLAYER_PRESENCE"
            assert alice_sees_bob["payload"]["state"] == "connected"

            assert bob_connected["type"] == "SESSION_EVENT"
            assert bob_connected["payload"]["event"] == "connected"

            # Bob claims a seat so he is a "seated" player
            ws_bob.send_json(
                {
                    "type": "PLAYER_SEAT",
                    "player_id": "bob",
                    "payload": {"character_slot": "grimble"},
                }
            )
            bob_seat_bob = ws_bob.receive_json()
            assert bob_seat_bob["type"] == "SEAT_CONFIRMED"
            # Alice also receives Bob's seat confirmed
            bob_seat_alice = ws_alice.receive_json()
            assert bob_seat_alice["type"] == "SEAT_CONFIRMED"

        # ws_bob context exited → Bob disconnected
        # Alice should receive PLAYER_PRESENCE{disconnected} then GAME_PAUSED
        presence_msg = ws_alice.receive_json()
        assert presence_msg["type"] == "PLAYER_PRESENCE"
        assert presence_msg["payload"]["state"] == "disconnected"
        assert presence_msg["payload"]["player_id"] == "bob"

        pause_msg = ws_alice.receive_json()
        assert pause_msg["type"] == "GAME_PAUSED", (
            f"Expected GAME_PAUSED after bob disconnects, got {pause_msg['type']}"
        )
        assert "bob" in pause_msg["payload"]["waiting_for"], (
            f"Expected 'bob' in waiting_for, got {pause_msg['payload']['waiting_for']}"
        )

        # Alice sends a PLAYER_ACTION — should get GAME_PAUSED back, NOT narration
        narrator_calls.clear()
        ws_alice.send_json(
            {
                "type": "PLAYER_ACTION",
                "player_id": "alice",
                "payload": {"action": "I look around the grimvault."},
            }
        )
        action_resp = ws_alice.receive_json()
        assert action_resp["type"] == "GAME_PAUSED", (
            f"Expected GAME_PAUSED in response to PLAYER_ACTION while paused, "
            f"got {action_resp['type']}"
        )
        assert "bob" in action_resp["payload"]["waiting_for"]
        assert len(narrator_calls) == 0, (
            "Narrator dispatch must NOT be called while the room is paused"
        )


# ---------------------------------------------------------------------------
# Test 2: Reconnect clears pause, GAME_RESUMED broadcast
# ---------------------------------------------------------------------------


def test_absent_player_reconnect_broadcasts_game_resumed(
    tmp_path: Path,
) -> None:
    """After bob disconnects and re-connects, alice sees GAME_RESUMED."""
    packs = _genre_packs_path()
    if packs is None:
        pytest.skip(f"No genre_packs directory found in {DEFAULT_GENRE_PACK_SEARCH_PATHS}")

    slug = _SLUG_BASE + "-resume"
    _seed(tmp_path, slug)
    app = create_app(genre_pack_search_paths=[packs], save_dir=tmp_path)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws_alice:
        ws_alice.send_json(
            {
                "type": "SESSION_EVENT",
                "player_id": "alice",
                "payload": {"event": "connect", "game_slug": slug},
            }
        )
        msg = ws_alice.receive_json()
        assert msg["type"] == "SESSION_EVENT"
        ws_alice.receive_json()  # CHARACTER_CREATION bootstrap

        ws_alice.send_json(
            {
                "type": "PLAYER_SEAT",
                "player_id": "alice",
                "payload": {"character_slot": "rux"},
            }
        )
        ws_alice.receive_json()  # SEAT_CONFIRMED

        # Bob connects and seats
        with client.websocket_connect("/ws") as ws_bob:
            ws_bob.send_json(
                {
                    "type": "SESSION_EVENT",
                    "player_id": "bob",
                    "payload": {"event": "connect", "game_slug": slug},
                }
            )
            ws_bob.receive_json()  # connected
            ws_bob.receive_json()  # CHARACTER_CREATION bootstrap
            ws_alice.receive_json()  # PLAYER_PRESENCE{connected} for bob

            ws_bob.send_json(
                {
                    "type": "PLAYER_SEAT",
                    "player_id": "bob",
                    "payload": {"character_slot": "grimble"},
                }
            )
            ws_bob.receive_json()  # bob sees SEAT_CONFIRMED
            ws_alice.receive_json()  # alice sees SEAT_CONFIRMED

        # Bob disconnected — alice receives PLAYER_PRESENCE then GAME_PAUSED
        presence_msg = ws_alice.receive_json()
        assert presence_msg["type"] == "PLAYER_PRESENCE"
        pause_msg = ws_alice.receive_json()
        assert pause_msg["type"] == "GAME_PAUSED"

        # Bob reconnects
        with client.websocket_connect("/ws") as ws_bob2:
            ws_bob2.send_json(
                {
                    "type": "SESSION_EVENT",
                    "player_id": "bob",
                    "payload": {"event": "connect", "game_slug": slug},
                }
            )
            ws_bob2.receive_json()  # bob sees SESSION_EVENT{connected}
            ws_bob2.receive_json()  # CHARACTER_CREATION bootstrap
            # alice should receive PLAYER_PRESENCE{connected} then GAME_RESUMED
            bob_reconnect_presence = ws_alice.receive_json()
            assert bob_reconnect_presence["type"] == "PLAYER_PRESENCE"
            assert bob_reconnect_presence["payload"]["state"] == "connected"

            game_resumed = ws_alice.receive_json()
            assert game_resumed["type"] == "GAME_RESUMED", (
                f"Expected GAME_RESUMED after bob reconnects, got {game_resumed['type']}"
            )
