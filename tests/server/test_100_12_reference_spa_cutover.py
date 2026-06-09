"""Story 100-12 (Phase 4, RED) — cutover: /reference/* HTML → React SPA.

This is the FINAL epic-100 story. The server gets out of the reference-HTML
business: the server-rendered `/reference/rules/{pack}` and
`/reference/lore/{pack}/{world}` HTML pages are retired and those URLs now
fall through to the SPA shell (history-fallback, spec C5), while the JSON
projection API (`/reference/api/*`) and the `reference_visibility.py`
firewall SURVIVE unchanged (the load-bearing safety net — C1).

Test groups:
  (a) HTML routes flip to SPA       — behavioral, currently RED
  (b) HTML emitters + islands.js retired — removal guards, currently RED
  (c) JSON API + firewall survive cutover — regression safety net (stays GREEN)

(c) is intentionally GREEN before AND after the cutover: it is the guard that
the destructive parts of this story do not take the surviving public contract
down with them. If a (c) test ever goes RED, the cutover broke the firewall or
the API — exactly what this net is here to catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sidequest.server.app import create_app

# --- A SPA-shell sentinel that can never appear in server-rendered reference
# HTML. If a /reference/rules|lore URL returns a body containing this, the
# request fell through to the SPA index.html (the post-cutover contract). ---
_SPA_SENTINEL = "SQ_SPA_SHELL_SENTINEL_100_12"

# Full palette so build_theme_tokens (the API path) can emit the required
# CSS-var token set — secondary/surface/text are required by the projector.
_MINIMAL_THEME_YAML = (
    "primary: '#5C7A4F'\n"
    "secondary: '#3A5236'\n"
    "accent: '#C9A96E'\n"
    "background: '#F4EBDA'\n"
    "surface: '#E8DCC0'\n"
    "text: '#2B2620'\n"
    "archetype: parchment\n"
    "web_font_family: Lora\n"
    "display_font_family: Playfair Display\n"
    "dinkus:\n  glyph:\n    light: '—'\n    medium: '❧'\n    heavy: '❧❧❧'\n"
)

# Server-rendered reference HTML markers that MUST NOT survive the cutover.
# "sleuth"/"deduce" are pack rules content; "Demoworld"/"a tale" are lore
# content; the islands script tag is the vanilla-JS hydration island.
_RULES_HTML_MARKERS = ("sleuth", "deduce")
_LORE_HTML_MARKERS = ("Demoworld", "a tale")
_ISLANDS_SCRIPT_TAG = '<script defer src="/reference/static/islands.js">'
# islands.js bundle source marker — present in the served bundle today.
_ISLANDS_BUNDLE_MARKER = "data-island"


def _seed_pack(packs_root: Path) -> None:
    """A minimal pack with a KEEPER-bearing npcs.yaml the firewall must drop."""
    pack = packs_root / "demo"
    world = pack / "worlds" / "demoworld"
    world.mkdir(parents=True)
    (pack / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    (pack / "archetypes.yaml").write_text("kinds:\n  - sleuth\n")
    (pack / "classes.yaml").write_text("amateur_sleuth:\n  signature: deduce\n")
    # npcs.yaml is keeper-tier — it must never cross the projection boundary.
    (pack / "npcs.yaml").write_text("villain: thedoctor\n")
    (world / "world.yaml").write_text("description: Demoworld is a rainy plateau.\n")
    (world / "legends.yaml").write_text("- name: A Tale\n  summary: a tale\n")


def _fake_ui_dist(root: Path) -> Path:
    """A hermetic Vite-style dist with a sentinel index.html + assets/ dir."""
    dist = root / "ui_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><title>SideQuest</title></head>"
        f'<body><div id="root"></div><script>{_SPA_SENTINEL}</script></body></html>'
    )
    (dist / "assets" / "app.js").write_text("// built bundle\n")
    return dist


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Full app (not just the reference router) so the SPA history-fallback
    catch-all is wired — the cutover relies on it serving `/reference/*`."""
    packs_root = tmp_path / "packs"
    packs_root.mkdir()
    _seed_pack(packs_root)
    dist = _fake_ui_dist(tmp_path)
    app = create_app(genre_pack_search_paths=[packs_root], ui_dist=dist)
    return TestClient(app)


# --------------------------------------------------------------------------
# (a) HTML routes flip to the SPA shell — currently RED.
# --------------------------------------------------------------------------


def test_rules_html_route_serves_spa_shell_not_server_html(client: TestClient) -> None:
    r = client.get("/reference/rules/demo")
    assert r.status_code == 200
    # Fell through to the SPA index.html…
    assert _SPA_SENTINEL in r.text
    # …and is NOT the retired server-rendered rules HTML.
    for marker in _RULES_HTML_MARKERS:
        assert marker not in r.text, f"server reference HTML leaked: {marker!r}"
    assert _ISLANDS_SCRIPT_TAG not in r.text


def test_lore_html_route_serves_spa_shell_not_server_html(client: TestClient) -> None:
    r = client.get("/reference/lore/demo/demoworld")
    assert r.status_code == 200
    assert _SPA_SENTINEL in r.text
    for marker in _LORE_HTML_MARKERS:
        assert marker not in r.text, f"server reference HTML leaked: {marker!r}"
    assert _ISLANDS_SCRIPT_TAG not in r.text


# --------------------------------------------------------------------------
# (b) HTML emitters + islands.js retired — removal guards, currently RED.
#     Reflection / filesystem / behavior guards (NOT source-text grep).
# --------------------------------------------------------------------------


def test_html_assembler_entrypoints_removed() -> None:
    """The two public HTML assemblers the routes called are deleted.

    Reflection guard (the legitimate exception to No Source-Text Wiring
    Tests). Non-vacuous: both attributes exist on develop today, so this
    FAILS until Dev retires the HTML assembly path.
    """
    import sidequest.server.reference_renderer as renderer

    assert not hasattr(renderer, "assemble_rules_page"), (
        "assemble_rules_page must be retired — server emits no reference HTML"
    )
    assert not hasattr(renderer, "assemble_lore_page"), (
        "assemble_lore_page must be retired — server emits no reference HTML"
    )


def test_islands_js_bundle_file_retired() -> None:
    """The vanilla-JS hydration island is deleted from the static tree."""
    islands = (
        Path(__file__).resolve().parents[2]
        / "sidequest"
        / "server"
        / "static"
        / "reference"
        / "islands.js"
    )
    assert not islands.exists(), f"islands.js must be deleted: {islands}"


def test_islands_js_no_longer_served(client: TestClient) -> None:
    """Whether the static route 404s or the SPA catch-all answers, the
    islands.js BUNDLE must no longer be served from /reference/static/."""
    r = client.get("/reference/static/islands.js")
    assert _ISLANDS_BUNDLE_MARKER not in r.text, (
        "islands.js bundle is still being served after cutover"
    )


# --------------------------------------------------------------------------
# (c) Surviving public contract — regression safety net (stays GREEN).
# --------------------------------------------------------------------------


def test_lore_api_survives_cutover(client: TestClient) -> None:
    r = client.get("/reference/api/lore/demo/demoworld")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    doc = r.json()
    assert doc.get("pack") == "demo"
    assert "sections" in doc


def test_rules_api_survives_cutover(client: TestClient) -> None:
    r = client.get("/reference/api/rules/demo")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    doc = r.json()
    assert doc.get("pack") == "demo"
    # Story 100-7: theme tokens ride on the projection doc for the SPA injector.
    assert "theme" in doc


def test_firewall_survives_cutover_keeper_absent(client: TestClient) -> None:
    """reference_visibility.py firewall is the load-bearing survivor (C1).

    The keeper-tier npcs.yaml villain name must not appear in EITHER public
    projection's serialized JSON. If the cutover relocated the projection but
    dropped the firewall, this catches the spoiler leak in the network tab.
    """
    lore = client.get("/reference/api/lore/demo/demoworld").text
    rules = client.get("/reference/api/rules/demo").text
    assert "thedoctor" not in lore, "keeper NPC leaked into lore projection"
    assert "thedoctor" not in rules, "keeper NPC leaked into rules projection"


def test_reference_visibility_module_intact() -> None:
    """The firewall module + its carve primitives survive the cutover."""
    import sidequest.server.reference_visibility as vis

    # The ADR-135 firewall primitives are kept verbatim per spec ("kept verbatim").
    assert hasattr(vis, "PUBLIC_STEMS")
    assert hasattr(vis, "KEEPER")
    assert callable(getattr(vis, "classify", None))
