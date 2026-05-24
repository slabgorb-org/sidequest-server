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


# --- HTML emission contract ---


def test_wrap_document_emits_data_pack_attribute(tmp_path):
    """Rendered HTML root has data-pack with the pack slug."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(tmp_path, "demo")
    html = assemble_rules_page("demo", pack_dir)

    assert 'data-pack="demo"' in html


def test_wrap_document_emits_data_archetype_attribute(tmp_path):
    """data-archetype comes from theme.yaml — drives per-archetype CSS rules."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(tmp_path, "demo")
    html = assemble_rules_page("demo", pack_dir)

    assert 'data-archetype="parchment"' in html


def test_wrap_document_emits_data_world_for_lore(tmp_path):
    """Lore pages get data-world attribute from the world slug."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack_dir = _write_pack(tmp_path, "demo")
    world_dir = pack_dir / "worlds" / "glenross"
    world_dir.mkdir(parents=True)
    (world_dir / "world.yaml").write_text("name: Glenross\n")

    html = assemble_lore_page("demo", "glenross", pack_dir, world_dir)

    assert 'data-pack="demo"' in html
    assert 'data-world="glenross"' in html


def test_wrap_document_injects_palette_css_vars(tmp_path):
    """Per-pack palette flows into the document as CSS custom properties so the
    static stylesheet can reference them without hardcoded hex values."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(tmp_path, "demo")
    html = assemble_rules_page("demo", pack_dir)

    # CSS variable form like --color-primary: #5C7A4F or similar
    assert "#5C7A4F" in html  # primary hex appears in inline style
    assert "#C9A96E" in html  # accent hex appears in inline style


def test_wrap_document_injects_font_family_tokens(tmp_path):
    """web_font_family + display_font_family must reach the browser via CSS vars or inline style."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(tmp_path, "demo")
    html = assemble_rules_page("demo", pack_dir)

    assert "Lora" in html
    assert "Playfair Display" in html


def test_wrap_document_loud_when_theme_missing(tmp_path):
    """If the pack lacks theme.yaml entirely, assemble_*_page must propagate
    MissingThemeFieldError — never silently emit a default-themed document."""
    from sidequest.server.reference_renderer import assemble_rules_page
    from sidequest.server.reference_theme import MissingThemeFieldError

    pack_dir = _write_pack(tmp_path, "demo", theme_yaml=None)

    with pytest.raises(MissingThemeFieldError):
        assemble_rules_page("demo", pack_dir)


def test_wrap_document_html_lang_preserved(tmp_path):
    """Pre-existing `lang="en"` attribute must survive Task 18 changes."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(tmp_path, "demo")
    html = assemble_rules_page("demo", pack_dir)

    assert 'lang="en"' in html


# --- Wiring test ---


def test_reference_theme_module_importable():
    """Wiring test: the production module must be importable. If the module
    doesn't exist or has import-time errors, this fails fast."""
    import sidequest.server.reference_theme as mod

    assert hasattr(mod, "load_reference_theme")
    assert hasattr(mod, "MissingThemeFieldError")
    assert hasattr(mod, "ReferenceTheme")
