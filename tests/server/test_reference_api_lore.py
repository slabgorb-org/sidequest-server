from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.server.reference_routes import create_reference_router


def _client(tmp_path: Path) -> TestClient:
    pack_dir = tmp_path / "pulp_noir"
    world_dir = pack_dir / "worlds" / "annees_folles"
    world_dir.mkdir(parents=True)
    (world_dir / "cartography.yaml").write_text(
        "starting_region: harbor\n"
        "regions:\n"
        "  harbor: {name: The Harbor, summary: Salt docks., description: Fog and hulls., adjacent: [market]}\n"
        "  market: {name: Night Market, summary: Lit stalls., description: Spice and smoke., adjacent: [harbor]}\n",
        encoding="utf-8",
    )
    app = FastAPI()
    app.state.genre_pack_search_paths = [str(tmp_path)]
    app.include_router(create_reference_router())
    return TestClient(app)


def test_lore_api_returns_map_section(tmp_path: Path):
    resp = _client(tmp_path).get("/reference/api/lore/pulp_noir/annees_folles")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["schema_version"] == 1
    assert [s["id"] for s in doc["sections"]] == ["map"]


def test_lore_api_404_unknown_world(tmp_path: Path):
    resp = _client(tmp_path).get("/reference/api/lore/pulp_noir/no_such_world")
    assert resp.status_code == 404


def test_lore_api_500_on_malformed_cartography(tmp_path: Path):
    c = _client(tmp_path)
    bad = tmp_path / "pulp_noir" / "worlds" / "annees_folles" / "cartography.yaml"
    bad.write_text("regions: [unclosed\n", encoding="utf-8")
    resp = c.get("/reference/api/lore/pulp_noir/annees_folles")
    assert resp.status_code == 500
