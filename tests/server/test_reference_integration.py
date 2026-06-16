"""End-to-end test: render a fixture pack via the live reference router.

Per project rules, no assertions against live genre_packs — only against
this fixture pack which is part of the test suite and version-controlled.
Story 54-3 / no-content-coupled-tests memo: pack-specific assertions
belong to the validator, not unit tests; this is an INTEGRATION test
hitting the reference router with a fixture, which is the allowed shape.

Fixture: tests/fixtures/packs/reference_v2_fixture/
  classes.yaml  — Knight, Burglar  (both with signature_ability sub-dict)
  cultures.yaml — Knight, Commoner (Knight shares name with the class)
  rules.yaml    — minimal core_rules + resolution keys
  worlds/long_fixture/
    world.yaml   — name + description
    legends.yaml — The Rending, The Long Winter
    locations.yaml — The Vicarage, The Old Mill

Cross-file collision invariant: class "Knight" must produce id="class-knight"
on the rules page; culture "Knight" must produce id="culture-knight" on the
lore page; neither anchor may appear on the wrong page.

locations.yaml is enumerated in LORE_WORLD_FILES (the renderer walks it
as a world-level lore file), so location-the-vicarage appears on the
lore page like legends and cultures.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.server.app import create_app

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "packs"
_PACK = "reference_v2_fixture"
_WORLD = "long_fixture"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app) as c:
        yield c


def _anchor_island(html: str) -> list[str]:
    match = re.search(
        r'<script id="ref-anchors" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "anchor island missing from rendered HTML"
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# HTTP 200 smoke
# ---------------------------------------------------------------------------


def test_rules_page_returns_200(client: TestClient) -> None:
    response = client.get(f"/reference/rules/{_PACK}")
    assert response.status_code == 200, response.text


def test_lore_page_returns_200(client: TestClient) -> None:
    response = client.get(f"/reference/lore/{_PACK}/{_WORLD}")
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Namespaced anchors on rules page
# ---------------------------------------------------------------------------


def test_rules_page_emits_class_knight_anchor(client: TestClient) -> None:
    response = client.get(f"/reference/rules/{_PACK}")
    anchors = _anchor_island(response.text)
    assert "class-knight" in anchors, f"class-knight not in anchor island: {anchors}"


def test_rules_page_emits_class_burglar_anchor(client: TestClient) -> None:
    response = client.get(f"/reference/rules/{_PACK}")
    anchors = _anchor_island(response.text)
    assert "class-burglar" in anchors, f"class-burglar not in anchor island: {anchors}"


# ---------------------------------------------------------------------------
# Namespaced anchors on lore page
# ---------------------------------------------------------------------------


def test_lore_page_emits_culture_knight_anchor(client: TestClient) -> None:
    response = client.get(f"/reference/lore/{_PACK}/{_WORLD}")
    anchors = _anchor_island(response.text)
    assert "culture-knight" in anchors, f"culture-knight not in anchor island: {anchors}"


def test_lore_page_emits_legend_the_rending_anchor(client: TestClient) -> None:
    response = client.get(f"/reference/lore/{_PACK}/{_WORLD}")
    anchors = _anchor_island(response.text)
    assert "legend-the-rending" in anchors, f"legend-the-rending not in anchor island: {anchors}"


def test_lore_page_emits_location_the_vicarage_anchor(client: TestClient) -> None:
    response = client.get(f"/reference/lore/{_PACK}/{_WORLD}")
    anchors = _anchor_island(response.text)
    assert "location-the-vicarage" in anchors, (
        f"location-the-vicarage not in anchor island: {anchors}"
    )


# ---------------------------------------------------------------------------
# Cross-file name-collision invariant
# ---------------------------------------------------------------------------


def test_class_knight_id_on_rules_page(client: TestClient) -> None:
    """id="class-knight" must appear on the rules page."""
    html = client.get(f"/reference/rules/{_PACK}").text
    assert 'id="class-knight"' in html, "class-knight id attribute missing from rules page HTML"


def test_culture_knight_id_on_lore_page(client: TestClient) -> None:
    """id="culture-knight" must appear on the lore page."""
    html = client.get(f"/reference/lore/{_PACK}/{_WORLD}").text
    assert 'id="culture-knight"' in html, "culture-knight id attribute missing from lore page HTML"


def test_class_knight_absent_from_lore_page(client: TestClient) -> None:
    """class-knight must NOT leak onto the lore page."""
    html = client.get(f"/reference/lore/{_PACK}/{_WORLD}").text
    assert 'id="class-knight"' not in html, (
        "class-knight id found on lore page — anchor collision between classes and cultures"
    )


def test_culture_knight_absent_from_rules_page(client: TestClient) -> None:
    """culture-knight must NOT leak onto the rules page."""
    html = client.get(f"/reference/rules/{_PACK}").text
    assert 'id="culture-knight"' not in html, (
        "culture-knight id found on rules page — anchor collision between cultures and classes"
    )


# ---------------------------------------------------------------------------
# JSON island content
# ---------------------------------------------------------------------------


def test_rules_anchor_island_contains_class_anchors(client: TestClient) -> None:
    html = client.get(f"/reference/rules/{_PACK}").text
    anchors = _anchor_island(html)
    for expected in ("class-knight", "class-burglar"):
        assert expected in anchors, f"{expected!r} missing from rules anchor island"


def test_lore_anchor_island_contains_culture_and_legend_anchors(
    client: TestClient,
) -> None:
    html = client.get(f"/reference/lore/{_PACK}/{_WORLD}").text
    anchors = _anchor_island(html)
    for expected in ("culture-knight", "legend-the-rending"):
        assert expected in anchors, f"{expected!r} missing from lore anchor island"


# ---------------------------------------------------------------------------
# Bad-anchor banner element
# ---------------------------------------------------------------------------


def test_bad_anchor_banner_present_on_rules_page(client: TestClient) -> None:
    html = client.get(f"/reference/rules/{_PACK}").text
    assert '<div id="ref-bad-anchor" hidden>' in html, (
        "bad-anchor banner div with id='ref-bad-anchor' missing from rules page"
    )
    assert "location.hash" in html, "location.hash inline script missing from rules page"


def test_bad_anchor_banner_present_on_lore_page(client: TestClient) -> None:
    html = client.get(f"/reference/lore/{_PACK}/{_WORLD}").text
    assert '<div id="ref-bad-anchor" hidden>' in html, (
        "bad-anchor banner div with id='ref-bad-anchor' missing from lore page"
    )
    assert "location.hash" in html, "location.hash inline script missing from lore page"


# ---------------------------------------------------------------------------
# 404 for unknown pack
# ---------------------------------------------------------------------------


def test_unknown_pack_returns_404(client: TestClient) -> None:
    response = client.get("/reference/rules/this_pack_does_not_exist")
    assert response.status_code == 404
