"""Integration: end-to-end party wiring test — drop → pause → reconnect → resume (MP-02 Task 9).

Verifies the complete flow:
1. Alice seats + connects.
2. Bob seats + connects.
3. Bob disconnects → alice sees PLAYER_PRESENCE{disconnected} then GAME_PAUSED.
4. Bob reconnects → alice sees PLAYER_PRESENCE{connected} then GAME_RESUMED.

Uses caverns_and_claudes / grimvault (genre/world available in the content repo).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.game.persistence import GameMode, SqliteStore, db_path_for_slug, upsert_game
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS
from sidequest.server.app import create_app

_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"
_SLUG_BASE = "2026-04-22-grimvault-party-wiring"


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


def test_drop_pauses_reconnect_resumes(tmp_path: Path) -> None:
    """Alice seats + connects, Bob seats + connects + drops.
    Alice sees GAME_PAUSED, then Bob reconnects and Alice sees GAME_RESUMED.
    """
    packs = _genre_packs_path()
    if packs is None:
        pytest.skip(f"No genre_packs directory found in {DEFAULT_GENRE_PACK_SEARCH_PATHS}")

    slug = _SLUG_BASE
    _seed(tmp_path, slug)
    app = create_app(genre_pack_search_paths=[packs], save_dir=tmp_path)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws_a:
        # Alice connects
        ws_a.send_json(
            {
                "type": "SESSION_EVENT",
                "player_id": "alice",
                "payload": {"event": "connect", "game_slug": slug},
            }
        )
        alice_connected = ws_a.receive_json()
        assert alice_connected["type"] == "SESSION_EVENT"
        assert alice_connected["payload"]["event"] == "connected"
        # Drain chargen bootstrap scene (slug path emits CHARACTER_CREATION
        # when has_character=False — playtest 2026-04-23 fix).
        alice_chargen = ws_a.receive_json()
        assert alice_chargen["type"] == "CHARACTER_CREATION"

        # Alice claims a seat
        ws_a.send_json(
            {
                "type": "PLAYER_SEAT",
                "player_id": "alice",
                "payload": {"character_slot": "rux"},
            }
        )
        alice_seat_confirmed = ws_a.receive_json()
        assert alice_seat_confirmed["type"] == "SEAT_CONFIRMED"

        # Bob connects and seats
        with client.websocket_connect("/ws") as ws_b:
            ws_b.send_json(
                {
                    "type": "SESSION_EVENT",
                    "player_id": "bob",
                    "payload": {"event": "connect", "game_slug": slug},
                }
            )
            bob_connected = ws_b.receive_json()
            assert bob_connected["type"] == "SESSION_EVENT"
            assert bob_connected["payload"]["event"] == "connected"
            # Drain Bob's chargen bootstrap scene.
            bob_chargen = ws_b.receive_json()
            assert bob_chargen["type"] == "CHARACTER_CREATION"

            # Alice receives PLAYER_PRESENCE{connected} for bob
            alice_sees_bob_connect = ws_a.receive_json()
            assert alice_sees_bob_connect["type"] == "PLAYER_PRESENCE"
            assert alice_sees_bob_connect["payload"]["state"] == "connected"

            # Bob claims a seat
            ws_b.send_json(
                {
                    "type": "PLAYER_SEAT",
                    "player_id": "bob",
                    "payload": {"character_slot": "grimble"},
                }
            )
            bob_seat_confirmed = ws_b.receive_json()
            assert bob_seat_confirmed["type"] == "SEAT_CONFIRMED"

            # Alice receives SEAT_CONFIRMED for bob
            alice_sees_bob_seat = ws_a.receive_json()
            assert alice_sees_bob_seat["type"] == "SEAT_CONFIRMED"

        # ws_b context exited → Bob disconnected
        # Alice should receive PLAYER_PRESENCE{disconnected} then GAME_PAUSED
        # Use a drain loop with slack for other messages that might arrive
        saw_pause = False
        for _ in range(5):
            m = ws_a.receive_json()
            if m["type"] == "GAME_PAUSED":
                saw_pause = True
                break
        assert saw_pause, "Expected GAME_PAUSED after bob disconnects"

        # Bob reconnects
        with client.websocket_connect("/ws") as ws_b2:
            ws_b2.send_json(
                {
                    "type": "SESSION_EVENT",
                    "player_id": "bob",
                    "payload": {"event": "connect", "game_slug": slug},
                }
            )
            # Drain bob's reconnect messages — expected set is:
            # {SESSION_EVENT(connected), CHARACTER_CREATION (bootstrap), GAME_RESUMED}.
            # Ordering across broadcast vs handler-return is racy, so just
            # assert the set of types received.
            seen_types: set[str] = set()
            for _ in range(3):
                m = ws_b2.receive_json()
                seen_types.add(m["type"])
            assert "SESSION_EVENT" in seen_types
            assert "GAME_RESUMED" in seen_types
            assert "CHARACTER_CREATION" in seen_types

            # Alice should receive PLAYER_PRESENCE{connected} then GAME_RESUMED
            saw_resume = False
            for _ in range(5):
                m = ws_a.receive_json()
                if m["type"] == "GAME_RESUMED":
                    saw_resume = True
                    break
            assert saw_resume, "Expected GAME_RESUMED after bob reconnects"
