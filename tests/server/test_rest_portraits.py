"""Tests for GET /api/chargen/portraits/{genre}/{world} (Epic 66).

Exercises the real genre-pack loader (load_genre_pack_cached) end-to-end
through the HTTP route. Fixture packs are built by copying the test_genre
fixture under a unique genre slug so the loader cache never collides between
tests.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.server.rest import create_rest_router

# ---------------------------------------------------------------------------
# Path to the canonical fixture pack that we clone for each test scenario.
# ---------------------------------------------------------------------------
_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"
_FIXTURE_WORLD = "flickering_reach"


def _clone_pack(
    dest_root: Path,
    genre_slug: str,
    portrait_manifest_yaml: str,
) -> Path:
    """Clone the test_genre fixture under *genre_slug* and overwrite the world's
    portrait_manifest.yaml with *portrait_manifest_yaml*.

    The lethality_policy.yaml in the clone has its ``genre_key`` updated to
    match the new slug (the loader validates the key matches the directory name).
    Returns the packs directory (parent of the genre directory).
    """
    packs_dir = dest_root / "genre_packs"
    genre_dir = packs_dir / genre_slug
    shutil.copytree(_FIXTURE_PACK, genre_dir)

    # Fix lethality_policy.yaml genre_key.
    lethality_yaml = genre_dir / "lethality_policy.yaml"
    if lethality_yaml.exists():
        data = yaml.safe_load(lethality_yaml.read_text(encoding="utf-8")) or {}
        data["genre_key"] = genre_slug
        lethality_yaml.write_text(yaml.dump(data), encoding="utf-8")

    # Overwrite portrait_manifest.yaml in the fixture world.
    manifest_path = genre_dir / "worlds" / _FIXTURE_WORLD / "portrait_manifest.yaml"
    manifest_path.write_text(portrait_manifest_yaml, encoding="utf-8")

    return packs_dir


def _make_client(packs_dir: Path) -> TestClient:
    """Build a minimal FastAPI app wired with create_rest_router() and return a
    TestClient.  No PG dependency — the portraits endpoint is stateless."""
    app = FastAPI()
    app.state.genre_pack_search_paths = [packs_dir]
    app.state.save_dir = packs_dir.parent  # unused by portraits route
    app.include_router(create_rest_router())
    return TestClient(app)


# ---------------------------------------------------------------------------
# Fixture scenarios
# ---------------------------------------------------------------------------

_MANIFEST_WITH_PICKERS = """\
characters:
  - name: Picker Alpha
    id: picker_a
    type: player_picker
    role: warrior
    culture: hegemonic
    archetype: ruler
    sex: female
  - name: Picker Beta
    id: picker_b
    type: player_picker
    role: rogue
    culture: frontier
    archetype: scoundrel
    sex: male
  - name: NPC Boss
    type: npc_major
    role: antagonist
    appearance: A looming figure in black armour.
"""

_MANIFEST_NO_PICKERS = """\
characters:
  - name: NPC Boss
    type: npc_major
    role: antagonist
    appearance: A looming figure in black armour.
"""


# ---------------------------------------------------------------------------
# Tests — world with player_picker entries
# ---------------------------------------------------------------------------


def test_list_pickers_filters_and_resolves(tmp_path: Path) -> None:
    """GET /api/chargen/portraits returns only player_picker entries with
    resolved portrait_url and correct metadata fields."""
    packs_dir = _clone_pack(tmp_path, "portraits_test_with_pickers", _MANIFEST_WITH_PICKERS)
    client = _make_client(packs_dir)

    resp = client.get(f"/api/chargen/portraits/portraits_test_with_pickers/{_FIXTURE_WORLD}")
    assert resp.status_code == 200
    body = resp.json()
    assert "portraits" in body

    slugs = {p["slug"] for p in body["portraits"]}
    assert slugs == {"picker_a", "picker_b"}, (
        f"expected only player_picker entries; got slugs={slugs}"
    )

    a = next(p for p in body["portraits"] if p["slug"] == "picker_a")
    assert a["portrait_url"].endswith(
        f"/genre_packs/portraits_test_with_pickers/worlds/{_FIXTURE_WORLD}/images/portraits/picker_a.png"
    ), f"unexpected portrait_url: {a['portrait_url']!r}"
    assert a["culture"] == "hegemonic"
    assert a["archetype"] == "ruler"


def test_list_pickers_npc_major_excluded(tmp_path: Path) -> None:
    """npc_major entries must not appear in the picker list."""
    packs_dir = _clone_pack(tmp_path, "portraits_test_npc_excluded", _MANIFEST_WITH_PICKERS)
    client = _make_client(packs_dir)

    resp = client.get(f"/api/chargen/portraits/portraits_test_npc_excluded/{_FIXTURE_WORLD}")
    assert resp.status_code == 200
    body = resp.json()
    slugs = {p["slug"] for p in body["portraits"]}
    assert "npc_boss" not in slugs and "NPC Boss" not in slugs


def test_list_pickers_response_shape(tmp_path: Path) -> None:
    """Every picker entry exposes slug, culture, archetype, sex, role, portrait_url."""
    packs_dir = _clone_pack(tmp_path, "portraits_test_shape", _MANIFEST_WITH_PICKERS)
    client = _make_client(packs_dir)

    resp = client.get(f"/api/chargen/portraits/portraits_test_shape/{_FIXTURE_WORLD}")
    assert resp.status_code == 200
    for entry in resp.json()["portraits"]:
        for field in ("slug", "culture", "archetype", "sex", "role", "portrait_url"):
            assert field in entry, f"field {field!r} missing from picker entry"


# ---------------------------------------------------------------------------
# Tests — world with no player_picker entries
# ---------------------------------------------------------------------------


def test_list_pickers_empty_world_returns_empty_list(tmp_path: Path) -> None:
    """A world with no player_picker entries returns 200 with an empty list —
    not an error (empty is a valid response per spec)."""
    packs_dir = _clone_pack(tmp_path, "portraits_test_no_pickers", _MANIFEST_NO_PICKERS)
    client = _make_client(packs_dir)

    resp = client.get(f"/api/chargen/portraits/portraits_test_no_pickers/{_FIXTURE_WORLD}")
    assert resp.status_code == 200
    assert resp.json() == {"portraits": []}


# ---------------------------------------------------------------------------
# Tests — unknown genre / world
# ---------------------------------------------------------------------------


def test_list_pickers_unknown_world_returns_empty_list(tmp_path: Path) -> None:
    """An unknown world within a valid genre returns 200 with an empty list
    (matches spec: empty is valid, unknown world is not an error)."""
    packs_dir = _clone_pack(tmp_path, "portraits_test_unknown_world", _MANIFEST_WITH_PICKERS)
    client = _make_client(packs_dir)

    resp = client.get("/api/chargen/portraits/portraits_test_unknown_world/no_such_world")
    assert resp.status_code == 200
    assert resp.json() == {"portraits": []}
