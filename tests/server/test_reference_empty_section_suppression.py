"""Story 63-11 — suppress empty Beat-Vocabulary + Achievements sections.

The reference page used to emit a ``<section id="…">`` for every TOC entry even
when its mapped files rendered nothing, plus a TOC nav link pointing at that
empty anchor. Census: Beat-Vocabulary is empty in 8/10 live packs, Achievements
in 9/10 — so most rules pages carried two placeholder sections and two dangling
nav links.

Decision (TEA-verified, Architect-ratified): SUPPRESS empty sections server-side
— drop BOTH the ``<section>`` AND its TOC entry. Two seams in
``reference_renderer.py`` are fixed:

1. ``_wrap_sections_by_toc`` — an empty section is dropped, not emitted.
2. ``_render_file`` — a presenter returning ``""`` (present-but-empty data,
   e.g. ``achievements: []`` or a beat_vocab carrying only the KEEPER-skipped
   ``obstacles`` key) suppresses the section instead of falling through to a
   ``<p><em>(empty)</em></p>`` placeholder.

Tests assert against the **served HTML artifact**. Suppression cases use a
synthetic pack (refactor-stable, no live-content coupling); the load-bearing
regression — real authored content MUST still render — drives the live packs
that actually carry the content (content-gated on ``SIDEQUEST_GENRE_PACKS``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sidequest.server.reference_renderer import assemble_rules_page

_MINIMAL_THEME_YAML = (
    "primary: '#5C7A4F'\n"
    "accent: '#C9A96E'\n"
    "background: '#F4EBDA'\n"
    "archetype: parchment\n"
    "web_font_family: Lora\n"
    "display_font_family: Playfair Display\n"
    "dinkus:\n  glyph:\n    light: '—'\n    medium: '❧'\n    heavy: '❧❧❧'\n"
)


def _packs_root() -> Path | None:
    """Resolve the genre_packs dir from ``SIDEQUEST_GENRE_PACKS`` (colon-
    separated), falling back to the orchestrator-relative content repo."""
    sentinel = "heavy_metal"
    for root in os.environ.get("SIDEQUEST_GENRE_PACKS", "").split(os.pathsep):
        if root and (Path(root) / sentinel).is_dir():
            return Path(root)
    repo_relative = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    if (repo_relative / sentinel).is_dir():
        return repo_relative
    return None


_PACKS_ROOT = _packs_root()

_live = pytest.mark.skipif(
    _PACKS_ROOT is None,
    reason="live genre packs not on SIDEQUEST_GENRE_PACKS path (content-gated)",
)


# =========================================================================
# (a) SUPPRESSION — empty sections + their TOC entries are dropped
# =========================================================================


def test_present_but_empty_achievements_section_suppressed(tmp_path):
    """present-but-empty case: a bare ``[]`` achievements.yaml (the real stub
    shape used by space_opera / elemental_harmony / neon_dystopia etc.) is
    present data that renders to nothing. The section AND its TOC link must be
    dropped, not rendered as a ``(empty)`` placeholder with a dangling anchor."""
    pack = tmp_path / "demo"
    pack.mkdir()
    (pack / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    (pack / "classes.yaml").write_text("- name: knight\n  signature: charge\n")
    (pack / "achievements.yaml").write_text("# stub, needs authoring\n[]\n")

    html = assemble_rules_page("demo", pack)

    # The empty Achievements section and its nav link are gone.
    assert '<section id="achievements">' not in html, "empty achievements section leaked"
    assert 'href="#achievements"' not in html, "dangling achievements TOC link leaked"
    # No placeholder fell through.
    assert "<p><em>(empty)</em></p>" not in html
    assert "<p><em>(empty file)</em></p>" not in html


def test_absent_beat_vocabulary_section_suppressed(tmp_path):
    """absent-file case: no beat_vocabulary.yaml at all. The Beat Vocabulary
    section and its TOC link must not appear."""
    pack = tmp_path / "demo"
    pack.mkdir()
    (pack / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    (pack / "classes.yaml").write_text("- name: knight\n  signature: charge\n")
    # No beat_vocabulary.yaml written.

    html = assemble_rules_page("demo", pack)

    assert '<section id="vocab">' not in html, "empty beat-vocab section leaked"
    assert 'href="#vocab"' not in html, "dangling beat-vocab TOC link leaked"


def test_suppression_is_surgical_nonempty_sections_survive(tmp_path):
    """Control: a page whose empty sections are dropped STILL renders its
    non-empty section + that section's TOC entry. Guards against the
    suppression eating the whole page (vacuous-pass protection)."""
    pack = tmp_path / "demo"
    pack.mkdir()
    (pack / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    (pack / "classes.yaml").write_text("- name: knight\n  signature: charge\n")
    (pack / "achievements.yaml").write_text("# stub, needs authoring\n[]\n")

    html = assemble_rules_page("demo", pack)

    # Characters section (classes → "bearing") renders, with its nav link.
    assert '<section id="bearing">' in html, "non-empty bearing section must survive"
    assert 'href="#bearing"' in html, "bearing TOC link must survive"
    assert 'id="class-knight"' in html, "real class content must render"


# =========================================================================
# (b) REGRESSION — real authored content STILL renders (load-bearing guard)
# =========================================================================


@_live
@pytest.mark.parametrize("pack", ["heavy_metal", "road_warrior"])
def test_nonempty_beat_vocabulary_still_renders(pack):
    """heavy_metal + road_warrior author real Beat-Vocabulary — suppression
    must NOT eat it. The section AND its TOC link must be present."""
    assert _PACKS_ROOT is not None
    html = assemble_rules_page(pack, _PACKS_ROOT / pack)

    assert '<section id="vocab">' in html, f"{pack} Beat-Vocabulary section was wrongly suppressed"
    assert 'href="#vocab"' in html, f"{pack} Beat-Vocabulary TOC link was wrongly dropped"


@_live
def test_nonempty_achievements_still_renders():
    """tea_and_murder authors real Achievements — suppression must NOT eat it."""
    assert _PACKS_ROOT is not None
    html = assemble_rules_page("tea_and_murder", _PACKS_ROOT / "tea_and_murder")

    assert '<section id="achievements">' in html, (
        "tea_and_murder Achievements was wrongly suppressed"
    )
    assert 'href="#achievements"' in html, (
        "tea_and_murder Achievements TOC link was wrongly dropped"
    )
