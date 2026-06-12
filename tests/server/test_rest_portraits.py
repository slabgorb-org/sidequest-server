"""Tests for GET /api/chargen/portraits/{genre}/{world} (Epic 66).

Exercises the real genre-pack loader (load_genre_pack_cached) end-to-end
through the HTTP route. Fixture packs come from the shared
``minimal_pack_factory`` conftest fixture (a clone of
tests/fixtures/packs/test_genre); each test overwrites the world's
portrait_manifest.yaml with its scenario.

The factory always clones to a directory named ``test_pack``, and
``load_genre_pack_cached`` keys its process-lifetime cache by genre code
string only — so the default cache is cleared around each test to keep one
test's pack from being served to the next (same slug, different tmp dirs).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.genre.loader import clear_default_cache
from sidequest.server.rest import create_rest_router

_WORLD = "flickering_reach"


@pytest.fixture(autouse=True)
def _isolate_genre_pack_cache():
    """load_genre_pack_cached caches by code string for the process lifetime;
    every test here uses the slug ``test_pack`` with different on-disk content,
    so the cache must not leak across tests."""
    clear_default_cache()
    yield
    clear_default_cache()


def _make_client_for_pack(pack_path: Path) -> TestClient:
    """Build a minimal FastAPI app wired with create_rest_router() and return a
    TestClient. No PG dependency — the portraits endpoint is stateless."""
    app = FastAPI()
    app.state.genre_pack_search_paths = [pack_path.parent]
    app.state.save_dir = pack_path.parent  # unused by portraits route
    app.include_router(create_rest_router())
    return TestClient(app)


def _write_manifest(pack_path: Path, manifest_yaml: str) -> None:
    manifest_path = pack_path / "worlds" / _WORLD / "portrait_manifest.yaml"
    manifest_path.write_text(manifest_yaml, encoding="utf-8")


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

# coyote_star shape: id-less pickers with space-separated names. The slug
# (and thus the URL filename) must be the generator's slugified form
# (scripts/generate_portrait_images._slugify_name), not the raw name.
_MANIFEST_PICKER_NO_ID = """\
characters:
  - name: picker hegemonic officer f01
    type: player_picker
    role: officer
    culture: hegemonic
    archetype: ruler
    sex: female
"""

_MANIFEST_NO_PICKERS = """\
characters:
  - name: NPC Boss
    type: npc_major
    role: antagonist
    appearance: A looming figure in black armour.
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_pickers_filters_and_resolves(minimal_pack_factory, tmp_path: Path) -> None:
    """GET /api/chargen/portraits returns only player_picker entries (npc_major
    excluded), each with the full response shape and a resolved portrait_url."""
    pack = minimal_pack_factory(tmp_path)
    _write_manifest(pack.path, _MANIFEST_WITH_PICKERS)
    client = _make_client_for_pack(pack.path)

    resp = client.get(f"/api/chargen/portraits/{pack.path.name}/{_WORLD}")
    assert resp.status_code == 200
    body = resp.json()
    assert "portraits" in body

    slugs = {p["slug"] for p in body["portraits"]}
    assert slugs == {"picker_a", "picker_b"}, (
        f"expected only player_picker entries (npc_major excluded); got slugs={slugs}"
    )

    for entry in body["portraits"]:
        for field in ("slug", "culture", "archetype", "sex", "role", "portrait_url"):
            assert field in entry, f"field {field!r} missing from picker entry"

    a = next(p for p in body["portraits"] if p["slug"] == "picker_a")
    # assets/portraits/ is the canonical world-portrait path the render
    # script writes (scripts/render_common.py) and emitters/_resolve_npc_
    # portrait_url resolves — NOT images/portraits/.
    assert a["portrait_url"].endswith(
        f"/genre_packs/{pack.path.name}/worlds/{_WORLD}/assets/portraits/picker_a.png"
    ), f"unexpected portrait_url: {a['portrait_url']!r}"
    assert a["culture"] == "hegemonic"
    assert a["archetype"] == "ruler"
    assert a["sex"] == "female"
    assert a["role"] == "warrior"


def test_list_pickers_idless_entry_slugifies_name(
    minimal_pack_factory, tmp_path: Path
) -> None:
    """An entry without an explicit ``id`` serves the generator-parity
    slugified name — slug and URL filename both — never the raw name
    (which would 404 against the rendered ``<slug>.png``)."""
    pack = minimal_pack_factory(tmp_path)
    _write_manifest(pack.path, _MANIFEST_PICKER_NO_ID)
    client = _make_client_for_pack(pack.path)

    resp = client.get(f"/api/chargen/portraits/{pack.path.name}/{_WORLD}")
    assert resp.status_code == 200
    portraits = resp.json()["portraits"]
    assert len(portraits) == 1
    entry = portraits[0]
    assert entry["slug"] == "picker_hegemonic_officer_f01"
    assert entry["portrait_url"].endswith(
        f"/genre_packs/{pack.path.name}/worlds/{_WORLD}/assets/portraits/"
        "picker_hegemonic_officer_f01.png"
    ), f"unexpected portrait_url: {entry['portrait_url']!r}"


def test_list_pickers_empty_world_returns_empty_list(
    minimal_pack_factory, tmp_path: Path
) -> None:
    """A world whose manifest ships only NPC entries returns 200 with an empty
    list — not an error (empty is a valid response per spec)."""
    pack = minimal_pack_factory(tmp_path)
    _write_manifest(pack.path, _MANIFEST_NO_PICKERS)
    client = _make_client_for_pack(pack.path)

    resp = client.get(f"/api/chargen/portraits/{pack.path.name}/{_WORLD}")
    assert resp.status_code == 200
    assert resp.json() == {"portraits": []}


def test_list_pickers_unknown_world_returns_empty_list(
    minimal_pack_factory, tmp_path: Path
) -> None:
    """An unknown world within a valid genre returns 200 with an empty list
    (matches spec: empty is valid, unknown world is not an error)."""
    pack = minimal_pack_factory(tmp_path)
    _write_manifest(pack.path, _MANIFEST_WITH_PICKERS)
    client = _make_client_for_pack(pack.path)

    resp = client.get(f"/api/chargen/portraits/{pack.path.name}/no_such_world")
    assert resp.status_code == 200
    assert resp.json() == {"portraits": []}
