"""RED tests for Story 65-11 — lore-page Map section (server-rendered SVG).

The lore reference page (``GET /reference/lore/{pack}/{world}``, ADR-135) is a
public table tool. 65-8 lit up its Points of Interest, 65-9 added the Cast
section. 65-11 adds a **Map** section: a server-rendered SVG **node-link graph**
built from the world's ``cartography.yaml`` — regions are nodes, each region's
``adjacent`` list is an edge, and npc-binding location entities
(``entities[].binding.kind == "npc"``) are portrait pins gated on R2 presence
the same way the Cast section gates portraits.

Cartography has **no coordinates** (every world is ``navigation_mode: region``
with only an adjacency list), so the layout is a *deterministic* node-link graph
computed from the adjacency, seeded at ``starting_region`` with ties broken by
sorted region id. The only genuinely new code is that layout + the section
renderer; the route, the section/TOC append, the R2 portrait gate, svgwrite, the
reference span family, and the test fixtures are all reused.

Observable contract pinned by these tests (drives Dev; NOT yet implemented — RED).
Assertions target this contract, never the source text (server CLAUDE.md: no
source-text wiring tests):

* ``assemble_lore_page`` appends a ``<section id="map">`` (heading "Map", one
  inline ``<svg>``) and a TOC entry ``{"id": "map", "label": "Map"}`` for any
  world that has a ``cartography.yaml``. A world without one renders unchanged
  (no Map section, HTTP 200). A malformed ``cartography.yaml`` fails loud (500).
* Each region renders one node carrying ``data-region-id="<region_id>"`` and the
  region ``name`` as its label. Node count == region count.
* Each adjacency renders one edge ``data-edge="<a>--<b>"`` where ``a``/``b`` are
  the two region ids sorted ascending. Reciprocal adjacency de-duplicates to a
  single edge. An adjacency to an unknown region id is skipped (no edge) and
  emits a ``sidequest.reference.map_dangling_edge`` WARN span.
* npc-binding entities render pins ``data-npc-slug="<slug>"``. A pin emits a
  portrait ``<img>`` iff ``portrait_image_key(pack, world, slug)`` is in
  ``r2_manifest.json`` (reused gate); otherwise a non-image marker, no broken
  ``<img>``. Non-npc / flavor_only entities never pin (ADR-135 public-only).
* Spans: one ``sidequest.reference.map_rendered`` per render carrying
  ``reference.map_node_count`` / ``map_edge_count`` / ``map_npc_pin_count`` /
  ``map_resolved_pin_count``; a per-pin ``map_pin_resolved`` / ``map_pin_not_found``
  pair (keyed by ``slug``); and the dangling-edge WARN span.
* The SVG is byte-deterministic: rendering the same cartography twice yields an
  identical map section, and node ordering is independent of YAML key order.
* Region/NPC names are HTML-escaped in the SVG (Python rule #11 / CWE-79).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.utils import slugify_player_name

# Span-capture helper lives in the server conftest (mirrors the 65-9 Cast test).
from tests.server.conftest import span_attrs_by_name

# --- Contract: span names (Dev registers these in FLAT_ONLY_SPANS) -----------
SPAN_MAP_RENDERED = "sidequest.reference.map_rendered"
SPAN_MAP_PIN_RESOLVED = "sidequest.reference.map_pin_resolved"
SPAN_MAP_PIN_NOT_FOUND = "sidequest.reference.map_pin_not_found"
SPAN_MAP_DANGLING_EDGE = "sidequest.reference.map_dangling_edge"

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "packs"
_PACK = "reference_v2_fixture"

_MAP_WORLD = "map_fixture"
_PERM_WORLD = "map_perm_fixture"
_MALFORMED_WORLD = "map_malformed_fixture"
_ESCAPE_WORLD = "map_escape_fixture"
_NO_CARTOGRAPHY_WORLD = "cast_gated_fixture"  # has portrait_manifest, no cartography.yaml

# The two npc-binding entities authored in map_fixture/cartography.yaml.
# Fixture invariant: slugify(label) == binding.ref == the r2 portrait slug.
_PRESENT_NAME = "Vivian Harbormaster"  # portrait key IS in tests/fixtures/r2_manifest.json
_ABSENT_NAME = "Thessaly Dunmore"  # npc entity, but portrait NOT on R2
_PRESENT_SLUG = slugify_player_name(_PRESENT_NAME)  # -> "vivian_harbormaster"
_ABSENT_SLUG = slugify_player_name(_ABSENT_NAME)  # -> "thessaly_dunmore"

# The four regions of map_fixture (node count) and the THREE valid de-duplicated
# edges (endpoints sorted ascending and joined with "--"). sunken_ghost is the
# dangling adjacency that must be dropped.
_REGIONS = {"harbor_light", "drowned_coast", "the_deeps", "tide_market"}
_EXPECTED_EDGES = {
    "drowned_coast--harbor_light",  # reciprocal pair -> single edge
    "harbor_light--the_deeps",
    "the_deeps--tide_market",
}
_DANGLING_REGION = "sunken_ghost"


@pytest.fixture
def gated_client() -> Iterator[TestClient]:
    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app) as c:
        yield c


# --- helpers: scope assertions to the Map section / its SVG ------------------


def _map_section(html: str) -> str:
    """Return the ``<section id="map"> ... </section>`` slice, or fail loudly.

    Scopes every map assertion to the map section so unrelated page chrome
    (other ``class=`` / ``<svg`` icons) can't satisfy an assertion by accident.
    RED today: there is no map section, so this fails with a clear message.
    """
    marker = 'id="map"'
    idx = html.find(marker)
    assert idx != -1, 'no Map section (id="map") in the rendered lore page'
    start = html.rfind("<section", 0, idx)
    assert start != -1, "Map section marker not inside a <section> element"
    end = html.index("</section>", idx)
    return html[start:end]


def _map_svg(html: str) -> str:
    """Return the ``<svg> ... </svg>`` slice inside the Map section."""
    section = _map_section(html)
    start = section.find("<svg")
    assert start != -1, "Map section has no inline <svg>"
    end = section.index("</svg>", start) + len("</svg>")
    return section[start:end]


def _region_order(svg: str) -> list[str]:
    """Ordered list of ``data-region-id`` values as emitted in the SVG."""
    return re.findall(r'data-region-id="([^"]+)"', svg)


def _edges(svg: str) -> list[str]:
    """All ``data-edge`` values emitted in the SVG."""
    return re.findall(r'data-edge="([^"]+)"', svg)


def _npc_slugs(svg: str) -> list[str]:
    """All ``data-npc-slug`` (pin) values emitted in the SVG."""
    return re.findall(r'data-npc-slug="([^"]+)"', svg)


# ---------------------------------------------------------------------------
# AC1 — Map section registered + rendered (present / graceful-absent / loud)
# ---------------------------------------------------------------------------


def test_map_section_present_for_world_with_cartography(gated_client: TestClient) -> None:
    """A world WITH cartography.yaml gains a Map section with an inline <svg> and
    a 'Map' TOC entry. RED today: assemble_lore_page never reads cartography."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    section = _map_section(resp.text)
    assert "<svg" in section, "Map section must contain an inline server-rendered <svg>"
    # TOC entry registered (label "Map"); the nav lists it.
    assert ">Map<" in resp.text or 'id="map"' in resp.text


def test_no_cartography_world_renders_without_map_section(gated_client: TestClient) -> None:
    """A world with NO cartography.yaml renders unchanged: HTTP 200, no Map
    section, no error (graceful — the map is purely additive)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_NO_CARTOGRAPHY_WORLD}")
    assert resp.status_code == 200, resp.text
    assert 'id="map"' not in resp.text, "world without cartography must have no Map section"


def test_malformed_cartography_fails_loud_500() -> None:
    """A world whose cartography.yaml is malformed must fail LOUD (HTTP 500),
    never a silently map-less 200 (No Silent Fallbacks). The fixture pack is used
    directly; ``raise_server_exceptions=False`` so an uncaught loud failure
    surfaces as a 500 *response* rather than re-raising into the test."""
    from sidequest.server.app import create_app

    app = create_app(genre_pack_search_paths=[FIXTURE_ROOT])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/reference/lore/{_PACK}/{_MALFORMED_WORLD}")
    assert resp.status_code == 500, (
        f"malformed cartography must fail loud (500), not a silent map-less page; "
        f"got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# AC2 — nodes from regions
# ---------------------------------------------------------------------------


def test_every_region_is_a_node(gated_client: TestClient) -> None:
    """Every region renders exactly one node; node count == region count, and
    each region's id is present (no region dropped, none invented)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    svg = _map_svg(resp.text)
    ids = _region_order(svg)
    assert len(ids) == len(_REGIONS), f"expected {len(_REGIONS)} nodes, got {len(ids)}: {ids}"
    assert set(ids) == _REGIONS, f"node ids must match the regions exactly: {ids}"


def test_region_name_is_the_node_label(gated_client: TestClient) -> None:
    """The human-readable region ``name`` appears in the SVG as the node label
    (not just the machine id)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    svg = _map_svg(resp.text)
    for label in ("Harbor Light", "Drowned Coast", "The Deeps", "Tide Market"):
        assert label in svg, f"region label {label!r} must be the node's text label"


# ---------------------------------------------------------------------------
# AC3 — edges from adjacency: de-dup reciprocal, skip + WARN on dangling
# ---------------------------------------------------------------------------


def test_edges_match_adjacency_with_reciprocal_dedup(gated_client: TestClient) -> None:
    """Adjacency renders edges; the reciprocal harbor_light<->drowned_coast pair
    collapses to a SINGLE de-duplicated edge (sorted-endpoint key). Exactly three
    valid edges, no duplicates."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    svg = _map_svg(resp.text)
    edges = _edges(svg)
    assert len(set(edges)) == len(edges), f"edges must be de-duplicated, found repeats: {edges}"
    assert set(edges) == _EXPECTED_EDGES, f"edge set mismatch: {sorted(edges)}"


def test_dangling_adjacency_is_skipped_and_warns(gated_client: TestClient, otel_capture) -> None:
    """An adjacency to an unknown region id (the_deeps -> sunken_ghost) draws NO
    edge and emits a ``map_dangling_edge`` WARN span naming the bad region — a
    fail-visible skip, not a silent drop and not a dangling edge."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    svg = _map_svg(resp.text)
    assert _DANGLING_REGION not in svg, "dangling region must not be drawn as a node/edge"
    for edge in _edges(svg):
        assert _DANGLING_REGION not in edge, f"no edge may reference the dangling region: {edge}"

    dangling = span_attrs_by_name(otel_capture, SPAN_MAP_DANGLING_EDGE)
    bad_regions = {a.get("reference.map_dangling_region") for a in dangling}
    assert _DANGLING_REGION in bad_regions, (
        "dangling adjacency must emit a map_dangling_edge WARN span naming sunken_ghost"
    )


# ---------------------------------------------------------------------------
# AC4 — deterministic layout (byte-identical; order independent of YAML order)
# ---------------------------------------------------------------------------


def test_map_svg_is_byte_deterministic(gated_client: TestClient) -> None:
    """Rendering the same cartography twice yields a byte-identical Map section —
    no time, no randomness, no dict-order leakage."""
    a = _map_section(gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}").text)
    b = _map_section(gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}").text)
    assert a == b, "map section must be byte-identical across renders (deterministic)"


def test_node_order_independent_of_yaml_key_order(gated_client: TestClient) -> None:
    """map_perm_fixture is the SAME graph as map_fixture with the region keys
    authored in a different order. The emitted node order must be IDENTICAL —
    layout derives from the graph + starting_region, not YAML insertion order."""
    base = _region_order(_map_svg(gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}").text))
    perm = _region_order(_map_svg(gated_client.get(f"/reference/lore/{_PACK}/{_PERM_WORLD}").text))
    assert base == perm, f"node order must be order-independent: {base} != {perm}"


# ---------------------------------------------------------------------------
# AC5 — npc portrait pins, gated through the existing R2 manifest gate
# ---------------------------------------------------------------------------


def test_both_npc_entities_render_as_pins(gated_client: TestClient) -> None:
    """Both npc-binding entities render pins (the gate decides the *image*, never
    whether the npc is pinned). Pins the source: npc-kind location entities."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    slugs = set(_npc_slugs(_map_svg(resp.text)))
    assert {_PRESENT_SLUG, _ABSENT_SLUG} <= slugs, f"both npc pins must render: {slugs}"


def test_on_r2_pin_emits_portrait_image(gated_client: TestClient) -> None:
    """A pin whose world-scoped portrait key IS in r2_manifest.json renders its R2
    ``<img>`` (the gate must not over-suppress real art)."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    svg = _map_svg(resp.text)
    expected_src = resolve_asset_url(
        f"genre_packs/{_PACK}/worlds/{_MAP_WORLD}/assets/portraits/{_PRESENT_SLUG}.png"
    )
    assert f'src="{expected_src}"' in svg, "on-R2 pin must render its portrait <img>"


def test_absent_portrait_pin_has_no_broken_image(gated_client: TestClient) -> None:
    """THE map portrait gate. Thessaly's pin exists but her portrait is NOT on R2,
    so NO ``<img>`` referencing her portrait key is emitted — a marker, never a
    broken image."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    svg = _map_svg(resp.text)
    absent_src = resolve_asset_url(
        f"genre_packs/{_PACK}/worlds/{_MAP_WORLD}/assets/portraits/{_ABSENT_SLUG}.png"
    )
    assert absent_src not in svg, "authored-but-not-on-R2 pin must emit no portrait <img>"
    # ...but her pin is still present (gate decides the image, not the pin).
    assert _ABSENT_SLUG in _npc_slugs(svg), "absent-portrait npc must still be pinned"


# ---------------------------------------------------------------------------
# AC6 — OTEL on every decision, with complement assertions
# ---------------------------------------------------------------------------


def test_map_rendered_span_carries_exact_counts(gated_client: TestClient, otel_capture) -> None:
    """One ``map_rendered`` span per render carrying the EXACT node/edge/pin
    counts so the GM/dev panel can confirm the renderer ran the graph rather than
    improvising: 4 nodes, 3 edges, 2 npc pins, 1 resolved pin."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    spans = span_attrs_by_name(otel_capture, SPAN_MAP_RENDERED)
    assert len(spans) == 1, f"expected exactly one map_rendered span, got {len(spans)}"
    attrs = spans[0]
    assert attrs.get("reference.map_node_count") == 4
    assert attrs.get("reference.map_edge_count") == 3
    assert attrs.get("reference.map_npc_pin_count") == 2
    assert attrs.get("reference.map_resolved_pin_count") == 1


def test_pin_decisions_emit_spans_with_complements(gated_client: TestClient, otel_capture) -> None:
    """Per-pin observability with COMPLEMENT assertions: the on-R2 pin is in
    resolved and NOT in not_found; the absent pin is in not_found and NOT in
    resolved. The complement is what distinguishes a correct gate from an
    always-resolve (or always-miss) gate."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    resolved = {a.get("slug") for a in span_attrs_by_name(otel_capture, SPAN_MAP_PIN_RESOLVED)}
    not_found = {a.get("slug") for a in span_attrs_by_name(otel_capture, SPAN_MAP_PIN_NOT_FOUND)}

    assert _PRESENT_SLUG in resolved, "on-R2 pin must fire map_pin_resolved"
    assert _PRESENT_SLUG not in not_found, "on-R2 pin must NOT fire map_pin_not_found"
    assert _ABSENT_SLUG in not_found, "absent pin must fire map_pin_not_found"
    assert _ABSENT_SLUG not in resolved, "absent pin must NOT fire map_pin_resolved"


# ---------------------------------------------------------------------------
# AC7 — public projection only: only npc entities pin
# ---------------------------------------------------------------------------


def test_only_npc_entities_pin(gated_client: TestClient) -> None:
    """ADR-135 public-only. flavor_only and location_feature entities are NOT
    pinned — exactly the two npc-binding entities pin, nothing else. Pins the
    leak where a non-npc entity (or a secret tier) shows up on the public map."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    slugs = _npc_slugs(_map_svg(resp.text))
    assert len(slugs) == 2, f"only the two npc entities may pin, got: {slugs}"
    assert "tide_salt_scales" not in slugs, "a location_feature entity must not pin"


# ---------------------------------------------------------------------------
# AC8 — wiring + regression + chrome classes
# ---------------------------------------------------------------------------


def test_existing_sections_unchanged_by_map_feature(gated_client: TestClient) -> None:
    """Regression: a world that has a Cast section (and no cartography) still
    renders its Cast section and 200 — the additive Map feature does not disturb
    existing sections."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_NO_CARTOGRAPHY_WORLD}")
    assert resp.status_code == 200, resp.text
    assert f'id="cast-{_PRESENT_SLUG}"' in resp.text, "existing Cast section must still render"


def test_map_emits_semantic_chrome_classes(gated_client: TestClient) -> None:
    """The Map SVG emits the semantic CSS classes the chrome contract expects so
    the chrome-wiring suite can validate them (closes the 65-13-style blind spot).
    Dev must add matching CSS rules + extend the chrome-wiring seed world."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_MAP_WORLD}")
    assert resp.status_code == 200, resp.text
    svg = _map_svg(resp.text)
    for css_class in ("ref-map__node", "ref-map__edge", "ref-map__pin"):
        assert css_class in svg, f"map SVG must emit the semantic class {css_class!r}"


# ---------------------------------------------------------------------------
# Python rule #11 (CWE-79) — HTML/SVG output must be escaped
# ---------------------------------------------------------------------------


def test_region_name_is_html_escaped_in_svg(gated_client: TestClient) -> None:
    """A region name with HTML-special characters must be escaped when
    interpolated into the SVG label (the way _cast_portrait_img_html escapes every
    interpolation). The raw ``<flag>`` must never appear unescaped."""
    resp = gated_client.get(f"/reference/lore/{_PACK}/{_ESCAPE_WORLD}")
    assert resp.status_code == 200, resp.text
    svg = _map_svg(resp.text)
    assert "Reef <flag>" not in svg, "raw HTML-special region name must not appear unescaped"
    assert "&lt;flag&gt;" in svg, "region name must be HTML-escaped in the SVG label"
