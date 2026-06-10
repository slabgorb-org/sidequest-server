"""Masthead meta block for the reference SPA (2026-06-09 redesign bundle).

``build_reference_meta`` assembles the chrome the React ``Masthead`` renders:
``pack_label`` + ``dateline`` from the ``reference_theme.py`` chrome constants
(re-wired after being stranded by the 100-12 cutover), and ``world_name`` from
the world's ``world.yaml`` on the lore tier. Gaps follow the documented chrome
loud-fallback doctrine: derive a humanized slug AND fire the
``sidequest.reference.meta_missing`` ERROR span — never silent, never a 500.

Wiring tests at the bottom prove the meta block actually rides the live
lore/rules API responses (route-layer attachment, same as ``theme``).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.server.reference_projection import build_reference_meta
from sidequest.server.reference_routes import create_reference_router

_MINIMAL_THEME_YAML = """\
primary: '#111111'
secondary: '#222222'
accent: '#333333'
background: '#444444'
surface: '#555555'
text: '#666666'
web_font_family: Georgia
display_font_family: Georgia
archetype: parchment
dinkus:
  glyph:
    light: "- * -"
    medium: "* * *"
    heavy: "* * * * *"
"""


# --- builder: authored chrome -------------------------------------------------


def test_known_pack_meta_uses_chrome_constants():
    meta = build_reference_meta("tea_and_murder")
    assert meta["pack_label"] == "Tea and Murder"
    assert meta["dateline"] == "the kettle is on and someone won't see breakfast"
    assert "world_name" not in meta  # pack tier — no world


def test_lore_tier_meta_reads_world_yaml_name(tmp_path: Path):
    world_dir = tmp_path / "glen"
    world_dir.mkdir()
    (world_dir / "world.yaml").write_text("name: Glenross\ndescription: a glen\n")
    meta = build_reference_meta("tea_and_murder", world_dir=world_dir)
    assert meta["world_name"] == "Glenross"


def test_every_live_pack_has_chrome_entries():
    """Every live pack in sidequest-content must have label + blurb chrome.

    Guards the 'adding a new live pack means adding an entry' contract in
    reference_theme.py — wry_whimsy was the first gap.
    """
    from sidequest.server.reference_theme import PACK_BLURBS, PACK_LABELS

    content_root = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    if not content_root.is_dir():
        # Subrepo layout not present (CI checkout of server only) — constants
        # self-consistency still holds below.
        live_packs = []
    else:
        live_packs = [p.name for p in content_root.iterdir() if (p / "theme.yaml").is_file()]
    for pack in live_packs:
        assert pack in PACK_LABELS, f"PACK_LABELS missing live pack {pack!r}"
        assert pack in PACK_BLURBS, f"PACK_BLURBS missing live pack {pack!r}"
    assert set(PACK_LABELS) == set(PACK_BLURBS)


# --- builder: loud fallback ----------------------------------------------------


def _spy_meta_span(monkeypatch):
    """Replace the meta_missing span helper in the projection module with a spy."""
    calls: list[dict] = []

    @contextmanager
    def spy(*, pack: str, field: str, _tracer=None):
        calls.append({"pack": pack, "field": field})
        yield MagicMock()

    import sidequest.server.reference_projection as mod

    monkeypatch.setattr(mod, "reference_meta_missing_span", spy)
    return calls


def test_unknown_pack_falls_back_humanized_and_fires_span(monkeypatch):
    calls = _spy_meta_span(monkeypatch)
    meta = build_reference_meta("demo_pack")
    assert meta["pack_label"] == "Demo Pack"
    assert "dateline" not in meta  # no blurb chrome → omitted, not invented
    fields = {c["field"] for c in calls}
    assert fields == {"pack_label", "dateline"}
    assert all(c["pack"] == "demo_pack" for c in calls)


def test_world_yaml_without_name_falls_back_humanized_and_fires_span(monkeypatch, tmp_path: Path):
    calls = _spy_meta_span(monkeypatch)
    world_dir = tmp_path / "demo_world"
    world_dir.mkdir()
    (world_dir / "world.yaml").write_text("description: nameless\n")
    meta = build_reference_meta("tea_and_murder", world_dir=world_dir)
    assert meta["world_name"] == "Demo World"
    assert {c["field"] for c in calls} == {"world_name"}


def test_missing_world_yaml_falls_back_humanized(monkeypatch, tmp_path: Path):
    calls = _spy_meta_span(monkeypatch)
    world_dir = tmp_path / "bare_world"
    world_dir.mkdir()
    meta = build_reference_meta("tea_and_murder", world_dir=world_dir)
    assert meta["world_name"] == "Bare World"
    assert {c["field"] for c in calls} == {"world_name"}


# --- span helper ----------------------------------------------------------------


def test_meta_missing_span_name_and_attrs():
    from sidequest.telemetry.spans.reference import reference_meta_missing_span

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_meta_missing_span(pack="demo", field="pack_label", _tracer=tracer):
        pass

    name = tracer.start_as_current_span.call_args[0][0]
    assert name == "sidequest.reference.meta_missing"


# --- wiring: meta rides the live API responses ----------------------------------


def _client(tmp_path: Path) -> TestClient:
    pack = tmp_path / "tea_and_murder"
    world = pack / "worlds" / "glenross"
    world.mkdir(parents=True)
    (pack / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    (pack / "rules.yaml").write_text("stat_generation: point_buy\n")
    (world / "world.yaml").write_text("name: Glenross\ndescription: a glen\n")
    app = FastAPI()
    app.state.genre_pack_search_paths = [tmp_path]
    app.include_router(create_reference_router())
    return TestClient(app)


def test_lore_api_carries_meta_block(tmp_path: Path):
    r = _client(tmp_path).get("/reference/api/lore/tea_and_murder/glenross")
    assert r.status_code == 200
    meta = r.json()["meta"]
    assert meta["pack_label"] == "Tea and Murder"
    assert meta["world_name"] == "Glenross"
    assert meta["dateline"] == "the kettle is on and someone won't see breakfast"


def test_rules_api_carries_pack_tier_meta_block(tmp_path: Path):
    r = _client(tmp_path).get("/reference/api/rules/tea_and_murder")
    assert r.status_code == 200
    meta = r.json()["meta"]
    assert meta["pack_label"] == "Tea and Murder"
    assert "world_name" not in meta
