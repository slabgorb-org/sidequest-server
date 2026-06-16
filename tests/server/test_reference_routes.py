"""HTTP boundary tests for the reference JSON projection API.

Story 100-12 (Phase 4 cutover) retired the server-rendered HTML routes
(``/reference/rules/*`` and ``/reference/lore/*``) and the
``/reference/static/*`` asset route — those URLs now fall through to the React
SPA. What remains under this router's ownership is the JSON projection API and
the shared pack/world resolver (404 with valid alternatives, path-traversal
rejection, fail-loud 500 on misconfiguration). These tests pin that surviving
boundary; the projection bodies themselves are covered by the
``test_reference_*_projection`` suite.
"""

from pathlib import Path

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


def _seed_pack(tmp_path: Path) -> None:
    pack = tmp_path / "demo"
    world = pack / "worlds" / "demoworld"
    world.mkdir(parents=True)
    (pack / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    (pack / "archetypes.yaml").write_text("kinds:\n  - sleuth\n")
    (pack / "classes.yaml").write_text("amateur_sleuth:\n  signature: deduce\n")
    (pack / "npcs.yaml").write_text("villain: thedoctor\n")  # MUST be excluded
    (world / "world.yaml").write_text("description: Demoworld is a rainy plateau.\n")
    (world / "legends.yaml").write_text("- name: A Tale\n  summary: a tale\n")


def test_rules_api_returns_json(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/api/rules/demo")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    doc = r.json()
    assert doc["pack"] == "demo"
    # Keeper-tier npcs.yaml must never cross the projection boundary (C1 firewall).
    assert "thedoctor" not in r.text


def test_lore_api_returns_json(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/api/lore/demo/demoworld")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    doc = r.json()
    assert doc["pack"] == "demo"
    assert "thedoctor" not in r.text


def test_unknown_pack_returns_404_with_valid_list(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/api/rules/nonesuch")
    assert r.status_code == 404
    assert "nonesuch" in r.text
    assert "demo" in r.text  # list of valid packs


def test_unknown_world_returns_404_with_valid_world_list(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/api/lore/demo/nonesuch")
    assert r.status_code == 404
    assert "nonesuch" in r.text
    assert "demoworld" in r.text


def test_pack_id_with_path_traversal_returns_404(tmp_path):
    _seed_pack(tmp_path)
    client = _build_app(tmp_path)
    r = client.get("/reference/api/rules/..%2Fevil")
    assert r.status_code == 404


def test_no_search_paths_configured_returns_500(tmp_path):
    app = FastAPI()
    app.state.genre_pack_search_paths = []
    app.include_router(create_reference_router())
    client = TestClient(app)
    r = client.get("/reference/api/rules/demo")
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

    r = client.get("/reference/api/rules/demo")
    assert r.status_code == 404
    assert "(none)" in r.text


def test_reference_api_registered_in_real_app(tmp_path):
    """Wiring test: the production app.py registers create_reference_router()
    and the JSON API is reachable from the real factory.

    Per CLAUDE.md doctrine — every test suite needs at least one test that
    verifies the component is reachable from production code paths.
    """
    from sidequest.server.app import create_app

    _seed_pack(tmp_path)
    app = create_app(genre_pack_search_paths=[tmp_path])
    client = TestClient(app)
    r = client.get("/reference/api/rules/demo")
    assert r.status_code == 200
    assert r.json()["pack"] == "demo"


def test_missing_theme_field_returns_500(tmp_path):
    """A pack with theme.yaml missing a required field (e.g., archetype) must
    surface as HTTP 500 on the JSON API. The theme token set rides on both
    projection docs (Story 100-7), so both API routes catch
    MissingThemeFieldError explicitly rather than 500-ing uncaught.
    """
    pack = tmp_path / "demo"
    world = pack / "worlds" / "demoworld"
    world.mkdir(parents=True)
    # archetype intentionally omitted — build_theme_tokens raises.
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
    r_rules = client.get("/reference/api/rules/demo")
    assert r_rules.status_code == 500
    assert "missing required field" in r_rules.text.lower()

    r_lore = client.get("/reference/api/lore/demo/demoworld")
    assert r_lore.status_code == 500
    assert "missing required field" in r_lore.text.lower()


def test_malformed_projection_returns_500_with_filename(tmp_path, monkeypatch):
    """When build_rules_projection raises ValueError (malformed YAML), the route
    must wrap it as 500 with the filename in the detail. Locks the from-exc
    chain on the surviving API route.
    """
    from sidequest.server import reference_routes

    def _boom(pack: str, *, pack_dir: Path) -> dict:
        raise ValueError("archetypes.yaml: malformed YAML: bad indent")

    _seed_pack(tmp_path)
    monkeypatch.setattr(reference_routes, "build_rules_projection", _boom)
    client = _build_app(tmp_path)

    r = client.get("/reference/api/rules/demo")
    assert r.status_code == 500
    assert "archetypes.yaml" in r.text
