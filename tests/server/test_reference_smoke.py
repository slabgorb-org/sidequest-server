"""Smoke test against the live tea_and_murder genre pack.

This is the FIXTURE-vs-LIVE separation called out in repo conventions: this
test asserts the JSON projection API returns 200 against the actually-shipping
content and that critical spoiler files do not leak across the projection
boundary. It does not assert specific content of any class or culture — that's
a content-team deliverable, not a server concern.

Story 100-12 (Phase 4 cutover) repointed these from the retired server-rendered
HTML routes (``/reference/{rules,lore}/*``) to the surviving JSON projection API
(``/reference/api/{rules,lore}/*``). The leak guard is now structural: keeper /
EXCLUDED file stems (``npcs``, ``seed_tropes``, ``tropes``, ``prompts``) must
never appear as a projected section id — the reference_visibility firewall +
EXCLUDED_FILES keep them out by construction.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _has_live_pack() -> bool:
    paths = os.environ.get("SIDEQUEST_GENRE_PACKS", "")
    for root in paths.split(os.pathsep):
        if root and (Path(root) / "tea_and_murder").is_dir():
            return True
    # Fallback: the orchestrator-relative path
    repo_relative = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    return (repo_relative / "tea_and_murder").is_dir()


pytestmark = pytest.mark.skipif(
    not _has_live_pack(),
    reason="live tea_and_murder pack not on SIDEQUEST_GENRE_PACKS path",
)


@pytest.fixture()
def client(monkeypatch):
    # The server-layer conftest installs an autouse fixture that points
    # DEFAULT_GENRE_PACK_SEARCH_PATHS at tests/fixtures/packs/. For this
    # smoke test we want the LIVE pack, so we re-bind the constant after
    # autouse has run (this fixture depends on monkeypatch and runs after
    # the autouse fixture has applied its patches).
    repo_relative = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    monkeypatch.setattr(
        "sidequest.genre.loader.DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [repo_relative],
    )
    if "SIDEQUEST_GENRE_PACKS" not in os.environ:
        monkeypatch.setenv("SIDEQUEST_GENRE_PACKS", str(repo_relative))
    from sidequest.server.app import create_app

    return TestClient(create_app(genre_pack_search_paths=[repo_relative]))


# Story 96-1: ``test_rules_route_against_live_tea_and_murder`` was RETIRED here
# rather than rewritten. It was doubly stale: (1) content-coupled — it asserted
# the live tea_and_murder pack ships archetypes/classes sections, which is a
# content requirement the content validator owns, not a server-test concern;
# (2) route-retired — it drove the server-rendered HTML route
# ``/reference/rules/{pack}``, deleted in the 100-12 SPA cutover. The surviving
# behavior (rules JSON projection renders classes/archetypes sections and
# firewalls keeper fields) is pinned fixture-first by
# ``tests/server/test_reference_rules_projection.py`` (story 100-6).

_KEEPER_STEMS = {"npcs", "seed_tropes", "tropes", "prompts"}


def test_live_lore_does_not_leak_npcs_or_seed_tropes(client):
    r = client.get("/reference/api/lore/tea_and_murder/glenross")
    assert r.status_code == 200
    doc = r.json()
    assert "sections" in doc
    # Keeper / EXCLUDED file stems must never surface as a projected section id.
    section_ids = {s.get("id") for s in doc["sections"]}
    leaked = section_ids & _KEEPER_STEMS
    assert not leaked, f"keeper file stems leaked into lore projection: {leaked}"


def test_live_rules_does_not_leak_seed_tropes(client):
    r = client.get("/reference/api/rules/tea_and_murder")
    assert r.status_code == 200
    doc = r.json()
    assert "sections" in doc
    section_ids = {s.get("id") for s in doc["sections"]}
    leaked = section_ids & _KEEPER_STEMS
    assert not leaked, f"keeper file stems leaked into rules projection: {leaked}"
