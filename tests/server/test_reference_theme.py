"""Tests for per-pack theme loading and HTML data-attribute injection (Task 18).

The reference pages emit `<html data-pack data-world data-archetype>` with
palette and font tokens from theme.yaml. Missing required fields raise
``MissingThemeFieldError`` loud — no silent fallback. AC1 + AC2 of story 63-4.

Fixtures use tmp-path packs only; never load live ``genre_packs/*`` per
project policy (no-content-coupled-tests). Live-pack coverage is the
validator's job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# --- Fixture helpers ---


_FULL_THEME_YAML = """\
primary: '#5C7A4F'
secondary: '#A67B5B'
accent: '#C9A96E'
background: '#F4EBDA'
surface: '#FDF6E8'
text: '#2C2417'
border_style: light
archetype: parchment
web_font_family: Lora
display_font_family: Playfair Display

dinkus:
  enabled: true
  glyph:
    light: "—  ❧  —"
    medium: "❧ ❧ ❧"
    heavy: "❧ ❧ ❧ ❧ ❧"
"""


def _write_pack(tmp_path: Path, name: str, theme_yaml: str | None = _FULL_THEME_YAML) -> Path:
    pack = tmp_path / name
    pack.mkdir(parents=True)
    if theme_yaml is not None:
        (pack / "theme.yaml").write_text(theme_yaml)
    (pack / "archetypes.yaml").write_text("kinds:\n  - sleuth\n")
    return pack


# --- Loader contract ---


def test_load_reference_theme_returns_dataclass(tmp_path):
    """Happy path: a full theme.yaml yields a ReferenceTheme with all required fields."""
    from sidequest.server.reference_theme import load_reference_theme

    pack_dir = _write_pack(tmp_path, "demo")
    theme = load_reference_theme(pack_dir)

    assert theme.archetype == "parchment"
    assert theme.web_font_family == "Lora"
    assert theme.display_font_family == "Playfair Display"


def test_load_reference_theme_exposes_palette(tmp_path):
    """Palette fields (primary/accent/background) must be addressable on the dataclass."""
    from sidequest.server.reference_theme import load_reference_theme

    pack_dir = _write_pack(tmp_path, "demo")
    theme = load_reference_theme(pack_dir)

    assert theme.palette_primary == "#5C7A4F"
    assert theme.palette_accent == "#C9A96E"
    assert theme.palette_background == "#F4EBDA"


def test_load_reference_theme_exposes_dinkus_glyphs(tmp_path):
    """Three dinkus glyph weights (light/medium/heavy) must be loadable."""
    from sidequest.server.reference_theme import load_reference_theme

    pack_dir = _write_pack(tmp_path, "demo")
    theme = load_reference_theme(pack_dir)

    assert theme.dinkus_light == "—  ❧  —"
    assert theme.dinkus_medium == "❧ ❧ ❧"
    assert theme.dinkus_heavy == "❧ ❧ ❧ ❧ ❧"


# --- Loud-failure contract (no silent fallbacks) ---


def test_missing_display_font_family_raises(tmp_path):
    """display_font_family is the 63-3 dependency; absent must raise MissingThemeFieldError."""
    from sidequest.server.reference_theme import (
        MissingThemeFieldError,
        load_reference_theme,
    )

    bad = _FULL_THEME_YAML.replace("display_font_family: Playfair Display\n", "")
    pack_dir = _write_pack(tmp_path, "demo", theme_yaml=bad)

    with pytest.raises(MissingThemeFieldError) as exc:
        load_reference_theme(pack_dir)
    assert "display_font_family" in str(exc.value)


def test_missing_web_font_family_raises(tmp_path):
    from sidequest.server.reference_theme import (
        MissingThemeFieldError,
        load_reference_theme,
    )

    bad = _FULL_THEME_YAML.replace("web_font_family: Lora\n", "")
    pack_dir = _write_pack(tmp_path, "demo", theme_yaml=bad)

    with pytest.raises(MissingThemeFieldError) as exc:
        load_reference_theme(pack_dir)
    assert "web_font_family" in str(exc.value)


def test_missing_archetype_raises(tmp_path):
    from sidequest.server.reference_theme import (
        MissingThemeFieldError,
        load_reference_theme,
    )

    bad = _FULL_THEME_YAML.replace("archetype: parchment\n", "")
    pack_dir = _write_pack(tmp_path, "demo", theme_yaml=bad)

    with pytest.raises(MissingThemeFieldError) as exc:
        load_reference_theme(pack_dir)
    assert "archetype" in str(exc.value)


def test_missing_palette_primary_raises(tmp_path):
    from sidequest.server.reference_theme import (
        MissingThemeFieldError,
        load_reference_theme,
    )

    bad = _FULL_THEME_YAML.replace("primary: '#5C7A4F'\n", "")
    pack_dir = _write_pack(tmp_path, "demo", theme_yaml=bad)

    with pytest.raises(MissingThemeFieldError) as exc:
        load_reference_theme(pack_dir)
    assert "primary" in str(exc.value)


def test_missing_dinkus_glyph_raises(tmp_path):
    """All three dinkus glyph weights are required; absence raises loud."""
    from sidequest.server.reference_theme import (
        MissingThemeFieldError,
        load_reference_theme,
    )

    bad = _FULL_THEME_YAML.replace('    medium: "❧ ❧ ❧"\n', "")
    pack_dir = _write_pack(tmp_path, "demo", theme_yaml=bad)

    with pytest.raises(MissingThemeFieldError) as exc:
        load_reference_theme(pack_dir)
    # Message should identify the missing key path (dinkus.glyph.medium or similar)
    assert "dinkus" in str(exc.value).lower() or "medium" in str(exc.value).lower()


def test_missing_theme_file_raises(tmp_path):
    """A pack with no theme.yaml at all must raise MissingThemeFieldError, not FileNotFoundError."""
    from sidequest.server.reference_theme import (
        MissingThemeFieldError,
        load_reference_theme,
    )

    pack_dir = _write_pack(tmp_path, "demo", theme_yaml=None)

    with pytest.raises(MissingThemeFieldError):
        load_reference_theme(pack_dir)


def test_empty_theme_field_treated_as_missing(tmp_path):
    """An empty string for a required field is as bad as omission — fail loud."""
    from sidequest.server.reference_theme import (
        MissingThemeFieldError,
        load_reference_theme,
    )

    bad = _FULL_THEME_YAML.replace(
        "display_font_family: Playfair Display",
        "display_font_family: ''",
    )
    pack_dir = _write_pack(tmp_path, "demo", theme_yaml=bad)

    with pytest.raises(MissingThemeFieldError) as exc:
        load_reference_theme(pack_dir)
    assert "display_font_family" in str(exc.value)


# --- HTML emission contract (RETIRED) ---
# Story 100-12 (Phase 4 cutover) retired the server-side HTML assemblers
# (``assemble_rules_page`` / ``assemble_lore_page``) and the ``_wrap_document``
# chrome. The per-pack theme now reaches the browser as a flat CSS-var token
# dict on the JSON projection (``build_theme_tokens``, top-level ``theme`` key),
# applied client-side by the SPA's session-free injector. The data-attribute /
# palette / font-token emission contract those tests pinned moved to
# ``test_reference_theme_projection.py`` (token set) and the React
# ``ReferenceLorePage.theme`` / ``ReferenceRulesPage.theme`` suites (injection).
# The loud-failure contract above (``load_reference_theme``) is unchanged.


# --- Wiring test ---


def test_reference_theme_module_importable():
    """Wiring test: the production module must be importable. If the module
    doesn't exist or has import-time errors, this fails fast."""
    import sidequest.server.reference_theme as mod

    assert hasattr(mod, "load_reference_theme")
    assert hasattr(mod, "MissingThemeFieldError")
    assert hasattr(mod, "ReferenceTheme")
