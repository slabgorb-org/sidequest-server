"""HTTP boundary tests for /reference/rules/* and /reference/lore/*.

These tests use a tmp-path pack so they do not depend on the live
sidequest-content tree. A separate smoke test (test_reference_smoke.py,
later) hits the live tea_and_murder pack.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.server.reference_routes import create_reference_router


def _build_app(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.genre_pack_search_paths = [tmp_path]
    app.include_router(create_reference_router())
    return TestClient(app)


_MINIMAL_THEME_YAML = (
    "primary: '#5C7A4F'\n"
    "accent: '#C9A96E'\n"
    "background: '#F4EBDA'\n"
    "archetype: parchment\n"
    "web_font_family: Lora\n"
    "display_font_family: Playfair Display\n"
    "dinkus:\n  glyph:\n    light: '—'\n    medium: '❧'\n    heavy: '❧❧❧'\n"
)


def _seed_pack(tmp_path: Path) -> None:
    pack = tmp_path / "demo"
    world = pack / "worlds" / "demoworld"
    world.mkdir(parents=True)
    (pack / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    (pack / "archetypes.yaml").write_text("kinds:\n  - sleuth\n")
    (pack / "classes.yaml").write_text("amateur_sleuth:\n  signature: deduce\n")
    (pack / "npcs.yaml").write_text("villain: thedoctor\n")  # MUST be excluded
    (world / "world.yaml").write_text("name: Demoworld\n")
    (world / "legends.yaml").write_text("legend: a tale\n")


def test_rules_route_returns_html(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/rules/demo")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "sleuth" in r.text
    assert "deduce" in r.text


def test_rules_route_excludes_npcs(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/rules/demo")
    assert "thedoctor" not in r.text


def test_lore_route_returns_html(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/lore/demo/demoworld")
    assert r.status_code == 200
    assert "Demoworld" in r.text
    assert "a tale" in r.text


def test_unknown_pack_returns_404_with_valid_list(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/rules/nonesuch")
    assert r.status_code == 404
    assert "nonesuch" in r.text
    assert "demo" in r.text  # list of valid packs


def test_unknown_world_returns_404_with_valid_world_list(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/lore/demo/nonesuch")
    assert r.status_code == 404
    assert "nonesuch" in r.text
    assert "demoworld" in r.text


def test_pack_id_with_path_traversal_returns_404(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/rules/..%2Fevil")
    assert r.status_code == 404


def test_no_search_paths_configured_returns_500(tmp_path):
    app = FastAPI()
    app.state.genre_pack_search_paths = []
    app.include_router(create_reference_router())
    client = TestClient(app)
    r = client.get("/reference/rules/demo")
    assert r.status_code == 500
    assert "search paths" in r.text.lower()


def test_missing_search_root_returns_404_not_500(tmp_path):
    """Spec contract: missing search root must produce a friendly 404 with
    'Valid packs: (none)', not an unhandled FileNotFoundError 500.
    """
    missing = tmp_path / "does-not-exist"
    app = FastAPI()
    app.state.genre_pack_search_paths = [missing]
    app.include_router(create_reference_router())
    client = TestClient(app)

    r = client.get("/reference/rules/demo")
    assert r.status_code == 404
    assert "(none)" in r.text


def test_stylesheet_route_serves_theme_css(tmp_path):
    """Story 63-4 Task 20 replaced the single ``reference.css`` with bundled
    ``theme.css`` + ``styles.css``. This test pins the theme.css path."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/static/theme.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


@pytest.mark.parametrize(
    "evil",
    [
        "EVIL.css",  # uppercase rejected by regex
        "..%2Fevil.css",  # URL-decoded traversal
        ".hidden",  # leading dot, no extension
        "/etc/passwd",  # absolute path attempt
        "no_extension",  # missing dotted extension
    ],
)
def test_stylesheet_route_rejects_bad_filenames(tmp_path, evil):
    """Any filename failing the safe-filename regex must 404, not 500 or 200."""
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get(f"/reference/static/{evil}")
    assert r.status_code == 404, f"Expected 404 for {evil!r}, got {r.status_code}"


def test_reference_router_registered_in_real_app(tmp_path):
    """Wiring test: the production app.py registers create_reference_router().

    Per CLAUDE.md doctrine — every test suite needs at least one test that
    verifies the component is reachable from production code paths.

    Uses ``create_app(genre_pack_search_paths=[tmp_path])`` (Option A) — the
    factory exposes a ``genre_pack_search_paths`` parameter directly, so we
    don't need to override ``app.state`` after construction or monkeypatch
    ``SIDEQUEST_GENRE_PACKS``.
    """
    from sidequest.server.app import create_app

    # Seed a minimal valid pack so the route returns 200.
    pack = tmp_path / "demo"
    pack.mkdir()
    (pack / "archetypes.yaml").write_text("kinds:\n  - sleuth\n")
    (pack / "theme.yaml").write_text(_MINIMAL_THEME_YAML)

    app = create_app(genre_pack_search_paths=[tmp_path])
    client = TestClient(app)
    r = client.get("/reference/rules/demo")
    assert r.status_code == 200
    assert "sleuth" in r.text


def test_missing_theme_field_returns_500(tmp_path):
    """A pack with theme.yaml missing a required field (e.g., archetype)
    must surface as HTTP 500. Regression guard: prior to Story 63-4 the
    routes caught only ValueError, so MissingThemeFieldError raised by
    load_reference_theme would have bubbled as an uncaught 500 with no
    log shape. Both routes now catch MissingThemeFieldError explicitly."""
    pack = tmp_path / "demo"
    world = pack / "worlds" / "demoworld"
    world.mkdir(parents=True)
    # archetype intentionally omitted — load_reference_theme raises.
    (pack / "theme.yaml").write_text(
        "primary: '#5C7A4F'\n"
        "accent: '#C9A96E'\n"
        "background: '#F4EBDA'\n"
        "web_font_family: Lora\n"
        "display_font_family: Playfair Display\n"
        "dinkus:\n  glyph:\n    light: '—'\n    medium: '❧'\n    heavy: '❧❧❧'\n"
    )
    (pack / "archetypes.yaml").write_text("kinds:\n  - sleuth\n")
    (world / "world.yaml").write_text("name: Demoworld\n")

    client = _build_app(tmp_path)
    r_rules = client.get("/reference/rules/demo")
    assert r_rules.status_code == 500
    assert "archetype" in r_rules.text.lower()

    r_lore = client.get("/reference/lore/demo/demoworld")
    assert r_lore.status_code == 500
    assert "archetype" in r_lore.text.lower()


def test_malformed_yaml_returns_500_with_filename(tmp_path, monkeypatch):
    """When assemble_rules_page raises ValueError (malformed YAML), the route
    must wrap it as 500 with the filename in the detail. Locks the from-exc
    chain.
    """
    from sidequest.server import reference_routes

    def _boom(pack: str, pack_dir: Path) -> str:
        raise ValueError("archetypes.yaml: malformed YAML: bad indent")

    _seed_pack(tmp_path)
    monkeypatch.setattr(reference_routes, "assemble_rules_page", _boom)
    client = _build_app(tmp_path)

    r = client.get("/reference/rules/demo")
    assert r.status_code == 500
    assert "archetypes.yaml" in r.text
