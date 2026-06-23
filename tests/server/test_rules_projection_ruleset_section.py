"""TDD tests for Task 5: ruleset_reference section prepended by build_rules_projection.

Story: docs/sdd/briefs/task-5-brief.md
Phase 1 (Fate SRD) — tests pin that build_rules_projection inserts a ``rules_document``
section at index 0 when the pack binds a named ruleset, and omits it for native/dial packs.

Test inventory:
  1. test_rules_projection_prepends_ruleset_section — when pack has ruleset: fate + SRD
     content, sections[0] is the rules_document section with the right id/type/chapter.
  2. test_rules_projection_no_section_for_native_pack — when pack has ruleset: dial (the
     native/vestigial fallback), no ruleset_reference section appears.
  3. test_rules_endpoint_emits_ruleset_section_on_wire — BEHAVIORAL wiring test: drives
     the real /reference/api/rules/{pack} route through a TestClient and asserts the
     ruleset_reference section reaches the wire. This replaces the plan's Step-5
     inspect.getsource source-text assertion, which is prohibited by server CLAUDE.md
     "No Source-Text Wiring Tests". A behavioral fixture-driven test through the real
     handler is the correct wiring proof per that rule.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.server.reference_projection import build_rules_projection
from sidequest.server.reference_routes import create_reference_router

# ---------------------------------------------------------------------------
# Shared SRD content seeder
# ---------------------------------------------------------------------------


def _seed_fate_content(content_root: Path) -> None:
    """Write a minimal fate SRD chapter under content_root/rulesets/fate/srd/."""
    srd = content_root / "rulesets" / "fate" / "srd"
    srd.mkdir(parents=True, exist_ok=True)
    (srd / "01.md").write_text(
        "---\n"
        "srd: fate\n"
        'srd_ref: "Basics"\n'
        "license: ccby\n"
        "anchor: fate-basics\n"
        "title: The Basics\n"
        "order: 1\n"
        "---\n"
        "How play works.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Minimal prod-faithful theme.yaml — rules endpoint requires it (No Silent
# Fallbacks: a theme-less pack 500s).
# ---------------------------------------------------------------------------

_THEME_YAML = (
    "archetype: terminal\n"
    "primary: '#4A90D9'\n"
    "secondary: '#2C5F8A'\n"
    "accent: '#E8A838'\n"
    "background: '#0D1117'\n"
    "surface: '#161B22'\n"
    "text: '#C9D1D9'\n"
    "web_font_family: Rajdhani\n"
    "display_font_family: Orbitron\n"
    "dinkus:\n"
    "  glyph: {light: '✦', medium: '✦ ⬡ ✦', heavy: '⬡ ✦ ⬡'}\n"
)


# ---------------------------------------------------------------------------
# Tests 1 & 2 — direct projector (behavioral seam, no HTTP)
# ---------------------------------------------------------------------------


def test_rules_projection_prepends_ruleset_section(tmp_path: Path) -> None:
    """build_rules_projection inserts rules_document at sections[0] for a fate pack."""
    content_root = tmp_path / "sidequest-content"
    pack_dir = content_root / "genre_packs" / "smoke_pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "rules.yaml").write_text("ruleset: fate\n", encoding="utf-8")
    _seed_fate_content(content_root)

    doc = build_rules_projection("smoke_pack", pack_dir=pack_dir)
    assert doc["sections"][0]["id"] == "ruleset_reference"
    assert doc["sections"][0]["type"] == "rules_document"
    assert doc["sections"][0]["chapters"][0]["anchor"] == "fate-basics"


def test_rules_projection_no_section_for_native_pack(tmp_path: Path) -> None:
    """build_rules_projection emits NO ruleset_reference section for ruleset: dial."""
    content_root = tmp_path / "sidequest-content"
    pack_dir = content_root / "genre_packs" / "native_pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "rules.yaml").write_text("ruleset: dial\n", encoding="utf-8")

    doc = build_rules_projection("native_pack", pack_dir=pack_dir)
    assert all(s["id"] != "ruleset_reference" for s in doc["sections"])


# ---------------------------------------------------------------------------
# Test 3 — behavioral wiring test through the real HTTP route
#
# This is a BEHAVIORAL wiring test (real route → section on the wire), replacing
# the plan's Step-5 inspect.getsource source-text assertion per server CLAUDE.md
# "No Source-Text Wiring Tests". Source-text wiring tests are banned because they
# pass when the literal is present even if the wiring is broken, and they fail on
# harmless refactors. Instead we drive the real route with a TestClient carrying
# the production reference router and assert the section appears in the JSON
# response body — proving end-to-end that the projection is called and its output
# reaches the wire.
# ---------------------------------------------------------------------------


def _wiring_client(content_root: Path, pack_slug: str) -> TestClient:
    """Build a TestClient with the production reference router, pack search path
    pointing at <content_root>/genre_packs."""
    pack_search = content_root / "genre_packs"
    app = FastAPI()
    app.state.genre_pack_search_paths = [str(pack_search)]
    app.include_router(create_reference_router())
    return TestClient(app)


def test_rules_endpoint_emits_ruleset_section_on_wire(tmp_path: Path) -> None:
    """GET /reference/api/rules/<pack> returns sections[0].id == 'ruleset_reference'
    when the pack binds fate and SRD content exists.

    Behavioral wiring test (fixture-driven, real route), replacing the plan's
    banned inspect.getsource source-text assertion per server CLAUDE.md
    'No Source-Text Wiring Tests'.
    """
    content_root = tmp_path / "sidequest-content"
    pack_slug = "wire_pack"
    pack_dir = content_root / "genre_packs" / pack_slug
    pack_dir.mkdir(parents=True, exist_ok=True)

    # Minimal pack files needed by the rules endpoint:
    # - rules.yaml with ruleset: fate (the field under test)
    # - theme.yaml (required by build_theme_tokens, no silent fallbacks)
    (pack_dir / "rules.yaml").write_text("ruleset: fate\n", encoding="utf-8")
    (pack_dir / "theme.yaml").write_text(_THEME_YAML, encoding="utf-8")

    # SRD content at the canonical location relative to pack_dir
    _seed_fate_content(content_root)

    client = _wiring_client(content_root, pack_slug)
    resp = client.get(f"/reference/api/rules/{pack_slug}")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    sections = resp.json()["sections"]
    assert sections, "sections must not be empty — rules_document section must reach the wire"
    first = sections[0]
    assert first["id"] == "ruleset_reference", (
        f"expected sections[0].id == 'ruleset_reference', got {first['id']!r}"
    )
    assert first["chapters"][0]["anchor"] == "fate-basics", (
        f"expected chapters[0].anchor == 'fate-basics', got {first['chapters'][0]['anchor']!r}"
    )
