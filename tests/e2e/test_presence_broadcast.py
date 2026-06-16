"""Integration: PLAYER_PRESENCE is broadcast when a second player joins.

Verifies that:
1. When alice connects to a multiplayer game, no PLAYER_PRESENCE is sent yet.
2. When bob connects to the same game, alice receives a PLAYER_PRESENCE{state=connected}.
3. The PLAYER_PRESENCE carries bob's player_id.

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
_SLUG = "2026-04-22-grimvault-mp"


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


def test_second_player_join_broadcasts_presence_to_first(tmp_path: Path):
    """Alice connects first; bob joins; alice receives PLAYER_PRESENCE{bob, connected}."""
    genre_packs_path: Path | None = next(
        (p for p in DEFAULT_GENRE_PACK_SEARCH_PATHS if p.exists()),
        None,
    )
    if genre_packs_path is None:
        pytest.skip(f"No genre_packs directory found in {DEFAULT_GENRE_PACK_SEARCH_PATHS}")

    _seed(tmp_path, _SLUG)
    app = create_app(
        genre_pack_search_paths=[genre_packs_path],
        save_dir=tmp_path,
    )
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws_a:
        ws_a.send_json(
            {
                "type": "SESSION_EVENT",
                "player_id": "alice",
                "payload": {"event": "connect", "game_slug": _SLUG},
            }
        )
        connected_msg = ws_a.receive_json()
        assert connected_msg["type"] == "SESSION_EVENT"
        assert connected_msg["payload"]["event"] == "connected"
        ws_a.receive_json()  # CHARACTER_CREATION bootstrap

        with client.websocket_connect("/ws") as ws_b:
            ws_b.send_json(
                {
                    "type": "SESSION_EVENT",
                    "player_id": "bob",
                    "payload": {"event": "connect", "game_slug": _SLUG},
                }
            )
            bob_connected = ws_b.receive_json()
            assert bob_connected["type"] == "SESSION_EVENT"
            assert bob_connected["payload"]["event"] == "connected"
            ws_b.receive_json()  # CHARACTER_CREATION bootstrap

            # Alice should now receive a PLAYER_PRESENCE about bob
            presence_msg = ws_a.receive_json()
            assert presence_msg["type"] == "PLAYER_PRESENCE", (
                f"Expected PLAYER_PRESENCE, got {presence_msg['type']}"
            )
            assert presence_msg["payload"]["player_id"] == "bob", (
                f"Expected player_id='bob', got {presence_msg['payload'].get('player_id')}"
            )
            assert presence_msg["payload"]["state"] == "connected", (
                f"Expected state='connected', got {presence_msg['payload'].get('state')}"
            )
