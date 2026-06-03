"""Tests for the bundled theme.css + styles.css static-asset route (Task 20).

The design-bundle CSS is copied to ``sidequest/server/static/reference/`` and
served via ``/reference/static/{filename}``. Both link tags must be emitted by
``_wrap_document``. The bundled CSS must contain zero ``.tweaks-*`` selectors
— those are design-tool affordances, not product (AC4).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.server.reference_routes import create_reference_router


def _build_app(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.genre_pack_search_paths = [tmp_path]
    app.include_router(create_reference_router())
    return TestClient(app)


def _seed_pack(tmp_path: Path) -> None:
    pack = tmp_path / "demo"
    pack.mkdir(parents=True)
    (pack / "archetypes.yaml").write_text("kinds:\n  - sleuth\n")
    (pack / "theme.yaml").write_text(
        "primary: '#5C7A4F'\n"
        "accent: '#C9A96E'\n"
        "background: '#F4EBDA'\n"
        "archetype: parchment\n"
        "web_font_family: Lora\n"
        "display_font_family: Playfair Display\n"
        "dinkus:\n"
        "  glyph:\n"
        "    light: '—'\n"
        "    medium: '❧'\n"
        "    heavy: '❧❧❧'\n"
    )


# --- Route surface ---


def test_static_theme_css_returns_200_with_css_content_type(tmp_path):
    """GET /reference/static/theme.css → 200 text/css. AC3 happy path."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/theme.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


def test_static_styles_css_returns_200_with_css_content_type(tmp_path):
    """GET /reference/static/styles.css → 200 text/css. AC3 happy path."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/styles.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


def test_static_theme_css_contains_token_vocabulary(tmp_path):
    """theme.css must declare CSS custom properties so the bundle's archetype
    overrides can resolve. A theme.css with no `--` would mean the bundle copy
    silently lost its design tokens."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/theme.css")
    assert r.status_code == 200
    assert "--" in r.text, "theme.css missing CSS custom properties"
    # Confirm at least one of the bundle's per-archetype variables survived
    # the copy — `--font-body` is defined under [data-archetype="parchment"]
    # in the source bundle. A truncated copy would lose it.
    assert "--font-body" in r.text


def test_static_theme_css_self_hosts_fonts_from_r2(tmp_path):
    """The reference-page stylesheet must self-host its fonts from R2 — no
    fonts.googleapis.com. Killing the Google @import without replacing it with
    @font-face would silently drop every display/body face to a serif fallback,
    so assert both: zero Google AND the CDN @font-face set is present."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/theme.css")
    assert r.status_code == 200
    assert "fonts.googleapis.com" not in r.text, "reference CSS still uses Google Fonts"
    assert "@font-face" in r.text, "reference CSS dropped its fonts entirely"
    assert "https://cdn.slabgorb.com/genre_packs/assets/fonts/" in r.text, (
        "reference @font-face must source faces from the R2 CDN"
    )
    # The folio body face (--folio-font-body: EB Garamond) must actually load.
    assert "EBGaramond" in r.text


def test_static_styles_css_contains_structural_rules(tmp_path):
    """styles.css must contain layout/structural rules so the page renders.
    An empty styles.css would mean the bundle copy was nuked."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/styles.css")
    assert r.status_code == 200
    # Sanity: at least one selector body present
    assert "{" in r.text and "}" in r.text
    assert len(r.text) > 200, "styles.css suspiciously small for a structural sheet"


# --- AC4: dead .tweaks-* selectors stripped ---


def test_static_theme_css_has_no_tweaks_selectors(tmp_path):
    """Regression guard: production theme.css must never reintroduce the design-tool
    affordance selectors (`.tweaks-panel`, `.tweaks-toggle`, `.tweaks-body`)."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/theme.css")
    assert r.status_code == 200
    body = r.text
    assert ".tweaks-panel" not in body
    assert ".tweaks-toggle" not in body
    assert ".tweaks-body" not in body


def test_static_styles_css_has_no_tweaks_selectors(tmp_path):
    """Same guard for styles.css."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/styles.css")
    assert r.status_code == 200
    body = r.text
    assert ".tweaks-panel" not in body
    assert ".tweaks-toggle" not in body
    assert ".tweaks-body" not in body


# --- _wrap_document link emission ---


def test_wrap_document_emits_theme_stylesheet_link(tmp_path):
    """Rendered HTML must <link> the per-pack theme stylesheet."""
    from sidequest.server.reference_renderer import assemble_rules_page

    _seed_pack(tmp_path)
    pack_dir = tmp_path / "demo"
    html = assemble_rules_page("demo", pack_dir)

    assert '<link rel="stylesheet" href="/reference/static/theme.css">' in html


def test_wrap_document_emits_styles_stylesheet_link(tmp_path):
    """Rendered HTML must <link> the structural styles sheet."""
    from sidequest.server.reference_renderer import assemble_rules_page

    _seed_pack(tmp_path)
    pack_dir = tmp_path / "demo"
    html = assemble_rules_page("demo", pack_dir)

    assert '<link rel="stylesheet" href="/reference/static/styles.css">' in html


def test_wrap_document_theme_link_precedes_styles_link(tmp_path):
    """Theme defines tokens; styles consume them. Theme must be loaded first so
    structural rules can resolve --color-primary etc."""
    from sidequest.server.reference_renderer import assemble_rules_page

    _seed_pack(tmp_path)
    pack_dir = tmp_path / "demo"
    html = assemble_rules_page("demo", pack_dir)

    theme_pos = html.index("theme.css")
    styles_pos = html.index("styles.css")
    assert theme_pos < styles_pos


# --- Negative paths ---


def test_static_unknown_filename_returns_404(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/does-not-exist.css")
    assert r.status_code == 404


def test_static_route_rejects_path_traversal(tmp_path):
    """The existing _SAFE_STATIC_FILENAME regex must continue to block traversal."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/..%2Fapp.py")
    assert r.status_code == 404
