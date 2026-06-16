"""Wiring tests for GET /api/games/{slug}/hub.

Sünden engine plan item 2. Exercises load_world_save() through a real
HTTP route — the production read-side consumer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.world_save import Hireling, WorldSave
from sidequest.server.app import create_app

_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture()
def content_client(tmp_path: Path) -> TestClient:
    if not _CONTENT_SEARCH_PATH.exists():
        pytest.skip("sidequest-content not on disk")
    app = create_app(
        save_dir=tmp_path,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    return TestClient(app)


def _seed_game(save_dir: Path, slug: str, genre: str, world: str) -> SqliteStore:
    db = db_path_for_slug(save_dir, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(store, slug=slug, mode=GameMode.SOLO, genre_slug=genre, world_slug=world)
    return store


def test_hub_endpoint_404_when_slug_missing(content_client: TestClient) -> None:
    r = content_client.get("/api/games/nope/hub")
    assert r.status_code == 404


def test_hub_endpoint_409_when_world_not_a_hub(
    content_client: TestClient,
    tmp_path: Path,
) -> None:
    _seed_game(tmp_path, "spgo-test", "space_opera", "coyote_star").close()
    r = content_client.get("/api/games/spgo-test/hub")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_a_hub_world"


def test_hub_endpoint_returns_world_save_and_dungeons(
    content_client: TestClient,
    tmp_path: Path,
) -> None:
    store = _seed_game(
        tmp_path,
        "cnc-test",
        "caverns_and_claudes",
        "caverns_sunden",
    )
    store.save_world_save(
        WorldSave(
            roster=[Hireling(id="vol_1", name="Volga", archetype="prig", stress=7)],
            currency=33,
            delve_count=2,
        )
    )
    store.close()

    r = content_client.get("/api/games/cnc-test/hub")
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "cnc-test"
    assert body["genre_slug"] == "caverns_and_claudes"
    assert body["world_slug"] == "caverns_sunden"
    dungeons = body["available_dungeons"]
    # Post–Sünden fold (2026-05-10): the three dungeons are cartography
    # regions on caverns_sunden tagged ``terrain: dungeon``. Endpoint
    # returns them alphabetically by region slug.
    assert [d["slug"] for d in dungeons] == [
        "grimvault_descent",
        "horden_warren",
        "mawdeep_gullet",
    ]
    assert {d["slug"]: d["sin"] for d in dungeons} == {
        "grimvault_descent": "pride",
        "horden_warren": "greed",
        "mawdeep_gullet": "gluttony",
    }
    assert all(d["wounded"] is False for d in dungeons)
    assert body["world_save"]["currency"] == 33
    assert body["world_save"]["delve_count"] == 2
    assert body["world_save"]["roster"][0]["name"] == "Volga"
    assert body["world_save"]["roster"][0]["stress"] == 7


def test_hub_endpoint_marks_wounded_dungeons(
    content_client: TestClient,
    tmp_path: Path,
) -> None:
    store = _seed_game(
        tmp_path,
        "cnc-wound",
        "caverns_and_claudes",
        "caverns_sunden",
    )
    store.save_world_save(
        WorldSave(
            dungeon_wounds={"grimvault_descent": True},
        )
    )
    store.close()

    r = content_client.get("/api/games/cnc-wound/hub")
    assert r.status_code == 200
    dungeons = {d["slug"]: d for d in r.json()["available_dungeons"]}
    assert dungeons["grimvault_descent"]["wounded"] is True
    assert dungeons["horden_warren"]["wounded"] is False
    assert dungeons["mawdeep_gullet"]["wounded"] is False


def test_hub_endpoint_fresh_hub_save_returns_empty_world_save(
    content_client: TestClient,
    tmp_path: Path,
) -> None:
    """Lazy-on-first-read: a hub save with no world_save row returns
    a default-populated WorldSave, not 404 / 500."""
    _seed_game(
        tmp_path,
        "cnc-fresh",
        "caverns_and_claudes",
        "caverns_sunden",
    ).close()
    r = content_client.get("/api/games/cnc-fresh/hub")
    assert r.status_code == 200
    body = r.json()
    assert body["world_save"]["roster"] == []
    assert body["world_save"]["currency"] == 0
    assert body["world_save"]["delve_count"] == 0
    assert [d["slug"] for d in body["available_dungeons"]] == [
        "grimvault_descent",
        "horden_warren",
        "mawdeep_gullet",
    ]
