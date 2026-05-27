"""RED tests for Story 65-2 AC4 — GET /api/sessions/{slug}/assets.

Exposes a save's asset_ledger to the UI for reconnect rehydration. Mirrors
tests/server/test_rest_encounter_events.py (same migrated-PG TestClient shape,
same slug→session_id resolution, same 404-on-unknown-slug contract).

Targets the not-yet-added route in sidequest/server/rest.py and the
not-yet-existing PgAssetLedgerStore.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.game import db_pool
from sidequest.game.pg import sessions as pg_sessions
from sidequest.server.rest import create_rest_router


@pytest.fixture
def pg_client(monkeypatch, migrated_db: str, tmp_path: Path):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()

    app = FastAPI()
    app.state.save_dir = tmp_path
    app.state.genre_pack_search_paths = []
    app.state.today_fn = lambda: date(2026, 4, 25)
    app.include_router(create_rest_router())
    client = TestClient(app)
    yield client, pool
    db_pool.close_pool()


def _seed_assets(pool, slug: str) -> int:
    from sidequest.game.pg.asset_ledger import PgAssetLedgerStore

    sid = pg_sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="test_genre", world_slug="test_world"
    )
    store = PgAssetLedgerStore(pool, session_id=sid)
    store.append(
        r2_key="artifacts/test_world/1/portrait/aaa.png",
        asset_type="portrait",
        entity_ref="gruk",
        created_turn=1,
    )
    store.append(
        r2_key="artifacts/test_world/1/illustration/bbb.png",
        asset_type="illustration",
        entity_ref="scene-2",
        created_turn=2,
    )
    return sid


def test_get_assets_returns_ledger_rows(pg_client) -> None:
    client, pool = pg_client
    slug = "assets-rest-basic"
    _seed_assets(pool, slug)

    resp = client.get(f"/api/sessions/{slug}/assets")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    keys = {row["r2_key"] for row in data}
    assert keys == {
        "artifacts/test_world/1/portrait/aaa.png",
        "artifacts/test_world/1/illustration/bbb.png",
    }


def test_get_assets_row_structure(pg_client) -> None:
    client, pool = pg_client
    slug = "assets-rest-structure"
    _seed_assets(pool, slug)

    resp = client.get(f"/api/sessions/{slug}/assets")
    assert resp.status_code == 200
    for row in resp.json():
        assert "r2_key" in row
        assert "asset_type" in row
        assert "entity_ref" in row
        assert "created_turn" in row
        # The endpoint resolves each r2_key to a browser-loadable absolute URL
        # (the raw key is not a fetchable source). Verify reuse-finding fix.
        assert "url" in row
        assert isinstance(row["url"], str) and row["url"].endswith(row["r2_key"])


def test_get_assets_404_for_unknown_slug(pg_client) -> None:
    """Unknown slug → 404 (loud), NOT an empty 200 (No Silent Fallbacks)."""
    client, _pool = pg_client
    resp = client.get("/api/sessions/does-not-exist/assets")
    assert resp.status_code == 404


def test_get_assets_empty_list_for_session_without_assets(pg_client) -> None:
    """Known session with no ledger rows → empty list, status 200."""
    client, pool = pg_client
    slug = "assets-rest-empty"
    pg_sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="test_genre", world_slug="test_world"
    )
    resp = client.get(f"/api/sessions/{slug}/assets")
    assert resp.status_code == 200
    assert resp.json() == []
