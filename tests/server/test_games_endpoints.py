# sidequest-server/tests/server/test_games_endpoints.py
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.server.rest import create_rest_router


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.save_dir = tmp_path
    app.state.genre_pack_search_paths = []
    app.state.today_fn = lambda: date(2026, 4, 22)  # injectable clock
    app.include_router(create_rest_router())
    return TestClient(app)


def test_post_games_creates_new_game(client: TestClient):
    r = client.post(
        "/api/games",
        json={
            "genre_slug": "low_fantasy",
            "world_slug": "moldharrow-keep",
            "mode": "multiplayer",
        },
    )
    assert r.status_code == 201
    body = r.json()
    # Multiplayer slugs carry a "-mp" suffix so they do not collide with
    # same-day solo games of the same world (no silent mode downgrade).
    assert body["slug"] == "2026-04-22-moldharrow-keep-mp"
    assert body["mode"] == "multiplayer"
    assert body["resumed"] is False


def test_post_games_same_mode_same_day_same_world_resumes(client: TestClient):
    first = client.post(
        "/api/games",
        json={
            "genre_slug": "low_fantasy",
            "world_slug": "moldharrow-keep",
            "mode": "multiplayer",
        },
    )
    assert first.status_code == 201
    second = client.post(
        "/api/games",
        json={
            "genre_slug": "low_fantasy",
            "world_slug": "moldharrow-keep",
            "mode": "multiplayer",
        },
    )
    assert second.status_code == 200  # resumed, not created
    body = second.json()
    assert body["slug"] == "2026-04-22-moldharrow-keep-mp"
    assert body["mode"] == "multiplayer"
    assert body["resumed"] is True


def test_post_games_solo_and_multiplayer_do_not_collide(client: TestClient):
    """Same world + same day in different modes must produce distinct games.

    Pre-fix bug: multiplayer was silently downgraded to whatever mode the
    existing same-day-same-world solo row carried.
    """
    solo = client.post(
        "/api/games",
        json={
            "genre_slug": "low_fantasy",
            "world_slug": "moldharrow-keep",
            "mode": "solo",
        },
    )
    assert solo.status_code == 201
    assert solo.json()["slug"] == "2026-04-22-moldharrow-keep"
    assert solo.json()["mode"] == "solo"

    mp = client.post(
        "/api/games",
        json={
            "genre_slug": "low_fantasy",
            "world_slug": "moldharrow-keep",
            "mode": "multiplayer",
        },
    )
    assert mp.status_code == 201  # new game, not a resume
    assert mp.json()["slug"] == "2026-04-22-moldharrow-keep-mp"
    assert mp.json()["mode"] == "multiplayer"
    assert mp.json()["resumed"] is False


def test_post_games_rejects_invalid_mode(client: TestClient):
    r = client.post(
        "/api/games",
        json={
            "genre_slug": "low_fantasy",
            "world_slug": "moldharrow-keep",
            "mode": "coop",
        },
    )
    assert r.status_code == 422


def test_get_games_slug_returns_metadata(client: TestClient):
    client.post(
        "/api/games",
        json={
            "genre_slug": "low_fantasy",
            "world_slug": "moldharrow-keep",
            "mode": "solo",
        },
    )
    r = client.get("/api/games/2026-04-22-moldharrow-keep")
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "2026-04-22-moldharrow-keep"
    assert body["mode"] == "solo"
    assert body["genre_slug"] == "low_fantasy"
    assert body["world_slug"] == "moldharrow-keep"


def test_get_games_slug_404_for_unknown(client: TestClient):
    r = client.get("/api/games/2026-01-01-nowhere")
    assert r.status_code == 404
