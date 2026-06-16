"""Integration: create a game via REST, then connect to it via WebSocket by slug.

This is the end-to-end wiring test for Multiplayer Plan 01 Task 9.
Verifies that:
1. POST /api/games creates a game and returns a slug
2. WebSocket can connect using SESSION_EVENT{connect, game_slug: ...}
3. Server loads the game and emits SESSION_EVENT{connected}
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sidequest.agents.claude_client import ClaudeResponse
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS
from sidequest.server.app import create_app


def _make_mock_client() -> MagicMock:
    """Create a mock LlmClient that returns canned narration."""
    mock = MagicMock()
    mock.send_with_session = AsyncMock(
        return_value=ClaudeResponse(
            text=(
                "The air is thick with possibility.\n\n"
                "```game_patch\n"
                '{"location": "The Cavern"}\n'
                "```"
            ),
            session_id="test-session-001",
            input_tokens=100,
            output_tokens=80,
        )
    )
    return mock


@pytest.fixture
def app_client(tmp_path: Path) -> TestClient:
    """Create a test app with mocked client and real genre pack search paths.

    Uses the actual genre_packs directory from the orchestrator root
    (sidequest-content/genre_packs) so we can test real worlds.
    """
    # Find the actual genre_packs directory — should be available via DEFAULT_GENRE_PACK_SEARCH_PATHS
    genre_packs_path: Path | None = next(
        (p for p in DEFAULT_GENRE_PACK_SEARCH_PATHS if p.exists()), None
    )

    if genre_packs_path is None:
        pytest.skip(f"No genre_packs directory found in {DEFAULT_GENRE_PACK_SEARCH_PATHS}")

    app = create_app(
        claude_client_factory=lambda: _make_mock_client(),
        genre_pack_search_paths=[genre_packs_path],
        save_dir=tmp_path,
    )
    # Injectable clock for stable slug generation
    app.state.today_fn = lambda: date(2026, 4, 22)
    return TestClient(app)


def test_create_game_then_connect_by_slug(app_client: TestClient):
    """End-to-end: POST /api/games → WS connect by slug → SESSION_EVENT{connected}."""
    # Step 1: Create a game via REST
    r = app_client.post(
        "/api/games",
        json={
            "genre_slug": "caverns_and_claudes",
            "world_slug": "grimvault",
            "mode": "multiplayer",
        },
    )
    assert r.status_code == 201, f"Failed to create game: {r.text}"
    slug = r.json()["slug"]
    assert slug == "2026-04-22-grimvault", f"Unexpected slug: {slug}"

    # Step 2: Connect to the game via WebSocket using the slug
    with app_client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "type": "SESSION_EVENT",
                "player_id": "alice",
                "payload": {"event": "connect", "game_slug": slug},
            }
        )
        msg = ws.receive_json()

        # Step 3: Verify we got a SESSION_EVENT{connected} message
        assert msg["type"] == "SESSION_EVENT", f"Expected SESSION_EVENT, got {msg['type']}"
        assert msg["payload"]["event"] == "connected", (
            f"Expected event='connected', got {msg['payload'].get('event')}"
        )
        assert msg["payload"]["genre"] == "caverns_and_claudes"
        assert msg["payload"]["world"] == "grimvault"
