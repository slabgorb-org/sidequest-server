"""Smoke test against the live tea_and_murder genre pack.

This is the FIXTURE-vs-LIVE separation called out in repo conventions: this
test asserts the route returns 200 against the actually-shipping content and
that critical spoiler files do not leak. It does not assert specific content
of any class or culture — that's a content-team deliverable, not a server
concern.
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


def test_rules_route_against_live_tea_and_murder(client):
    r = client.get("/reference/rules/tea_and_murder")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # Spec ACs: archetypes and classes are non-optional for tea_and_murder
    assert "archetypes.yaml" in r.text
    assert "classes.yaml" in r.text


def test_live_lore_does_not_leak_npcs_or_seed_tropes(client):
    r = client.get("/reference/lore/tea_and_murder/glenross")
    assert r.status_code == 200
    # File-level exclusion (v1) — even if files exist they must not render.
    # The renderer emits each rendered file as a `<section class="file" ...>`
    # with an `<h1>{filename}</h1>` heading (see reference_renderer.py). We
    # assert that no such file-section is emitted for spoiler files, rather
    # than a substring match (which would false-positive on prose that
    # references a path, e.g. a manifest description listing
    # `worlds/<world>/npcs.yaml — 12 NPCs`).
    assert "<h1>npcs.yaml</h1>" not in r.text
    assert "<h1>seed_tropes.yaml</h1>" not in r.text


def test_live_rules_does_not_leak_seed_tropes(client):
    r = client.get("/reference/rules/tea_and_murder")
    assert r.status_code == 200
    assert "<h1>seed_tropes.yaml</h1>" not in r.text
    assert "<h1>prompts.yaml</h1>" not in r.text
