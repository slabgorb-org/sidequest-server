"""Story 63-10 — Lore surface renders WORLD lore only (RED phase).

Architect ratified **Option #1, absolute world-only**: drop
``LORE_PACK_FLAVOR_FILES`` from ``assemble_lore_page`` entirely so the lore page
renders only world-tier files (``LORE_WORLD_FILES`` from ``world_dir``). No
pack-tier flavor is concatenated; the ``(genre)`` label disappears.

The live breaking case is ``/reference/lore/caverns_and_claudes/beneath_sunden``:
the pack's Keeper/Maw cosmology + "The dungeon is the world" banner currently
merge on top of a world whose own lore explicitly rejects Keepers/Maw. These
tests pin the cut against the **served HTML artifact** returned by
``assemble_lore_page`` (real content for the world-specific ACs; a synthetic
pack for the refactor-stable tier-isolation assertion, per the
No-Source-Text-Wiring-Tests rule).

Per Architect: **no OTEL span** — deterministic page assembly is cosmetic-scope
exempt; do not invent one.

AC→test map:
- AC1 test_ac1_pack_cosmology_absent          (RED now: pack lore leaks)
- AC2 test_ac2_pack_cultures_absent           (RED now: pack cultures leak)
- AC3 test_ac3_no_genre_label                 (RED now: "(genre)" present)
- AC4 test_ac4_world_voice_present            (regression-positive; green now+after)
- AC5 test_ac5_pack_flavor_not_merged_synthetic (RED now: sentinel leaks)
- AC6 test_ac6_world_tier_renders_no_cultures_section (RED now: pack cultures section)
- AC7 test_ac7_lore_pack_flavor_files_removed  (RED now: constant exists)
      test_ac7_public_stems_unchanged          (orthogonal-allowlist guard; green)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sidequest.server import reference_renderer
from sidequest.server.reference_renderer import assemble_lore_page

PACK = "caverns_and_claudes"
WORLD = "beneath_sunden"

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
    """Resolve the genre_packs dir holding caverns_and_claudes from
    ``SIDEQUEST_GENRE_PACKS`` (colon-separated), falling back to the
    orchestrator-relative content repo. Mirrors test_reference_smoke.py."""
    for root in os.environ.get("SIDEQUEST_GENRE_PACKS", "").split(os.pathsep):
        if root and (Path(root) / PACK).is_dir():
            return Path(root)
    repo_relative = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    if (repo_relative / PACK).is_dir():
        return repo_relative
    return None


_PACKS_ROOT = _packs_root()

_live = pytest.mark.skipif(
    _PACKS_ROOT is None,
    reason="live caverns_and_claudes pack not on SIDEQUEST_GENRE_PACKS path (content-gated)",
)


@pytest.fixture
def lore_html() -> str:
    """The served HTML for the real beneath_sunden lore page. Drives
    ``assemble_lore_page`` directly with the real pack/world dirs (the function
    reads the dirs it is handed — no loader/route indirection)."""
    assert _PACKS_ROOT is not None
    pack_dir = _PACKS_ROOT / PACK
    world_dir = pack_dir / "worlds" / WORLD
    return assemble_lore_page(PACK, WORLD, pack_dir, world_dir)


# =========================================================================
# AC1 — Pack cosmology / banner intro gone
# =========================================================================


@_live
def test_ac1_pack_cosmology_absent(lore_html):
    """The pack-tier Keeper cosmology, the_maw section, and the "dungeon is the
    world" setting_anchor banner must NOT render on the world lore page."""
    assert "The Keepers are the intelligences that dwell within" not in lore_html
    assert "Every dungeon has a Maw" not in lore_html
    assert "The dungeon is the world" not in lore_html


# =========================================================================
# AC2 — Pack cultures gone
# =========================================================================


@_live
def test_ac2_pack_cultures_absent(lore_html):
    """Pack-tier culture names (from pack cultures.yaml) must not leak — the
    world has no Keepers, so "Keeper Titles" / "Surface Folk" are nonsense on
    this page."""
    assert "Keeper Titles" not in lore_html
    assert "Surface Folk" not in lore_html


# =========================================================================
# AC3 — No "(genre)" tier label
# =========================================================================


@_live
def test_ac3_no_genre_label(lore_html):
    """No "(genre)" label suffix anywhere in the page.

    NOTE: this already holds today for caverns_and_claudes — the suffix is
    suppressed for *presented* stems (``_render_file_with_label`` returns early
    when the stem has a presenter, and lore/cultures both do), so the label was
    never visible even while the pack CONTENT leaked (AC1/AC2). Kept as a guard
    so the label can never reappear after the cut."""
    assert "(genre)" not in lore_html


# =========================================================================
# AC4 — World voice present (regression-positive; green now and after)
# =========================================================================


@_live
def test_ac4_world_voice_present(lore_html):
    """World-tier content (LORE_WORLD_FILES, unchanged) must still render: the
    world history/geography fragments and the world_name.

    Fragments are chosen mid-sentence so they survive the history presenter's
    dropcap (it wraps the first letter of a block in a
    ``<span class="ref-pull-quote__dropcap">`` — e.g. "Sünden Deep was a working
    hold" renders as ``…>S</span>ünden Deep was a working hold``, so the leading
    phrase is not a contiguous substring)."""
    assert "was a working hold" in lore_html  # world history (beneath_sunden lore.yaml)
    assert "no second mouth" in lore_html  # world geography
    assert "Beneath Sünden" in lore_html  # world_name (hero + lore)


# =========================================================================
# AC5 — Tier isolation via SYNTHETIC pack (refactor-stable, no live content)
# =========================================================================


def test_ac5_pack_flavor_not_merged_synthetic(tmp_path):
    """Behavioral tier-isolation. A synthetic pack-tier lore.yaml + cultures.yaml
    carry a unique sentinel; the world-tier files do not. After the cut the
    sentinel must be ABSENT from the served HTML — proving pack_dir flavor is
    not read/merged, without grepping production source."""
    sentinel = "PACKFLAVORSENTINEL_Zx9q_DO_NOT_RENDER"
    pack_dir = tmp_path / "synthpack"
    world_dir = pack_dir / "worlds" / "synthworld"
    world_dir.mkdir(parents=True)
    (pack_dir / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    # Pack-tier flavor files (the ones 63-7's merge pulled): they carry the sentinel.
    (pack_dir / "lore.yaml").write_text(f"setting_anchor: '{sentinel} banner'\n")
    (pack_dir / "cultures.yaml").write_text(f"- name: '{sentinel} Culture'\n  summary: pack flavor\n")
    # World-tier files — clean, no sentinel.
    (world_dir / "world.yaml").write_text("description: A clean synthetic world.\n")
    (world_dir / "lore.yaml").write_text("world_name: Synth World\nhistory: 'Only the world voice here.'\n")

    html = assemble_lore_page("synthpack", "synthworld", pack_dir, world_dir)

    assert sentinel not in html, "pack-tier flavor leaked into the world lore page"
    # Sanity: the world-tier content DID render (proves the page isn't just empty).
    # Dropcap-immune fragment: the history presenter wraps the leading "O" of
    # "Only…" in a <span class="ref-pull-quote__dropcap">, so assert on the
    # mid-sentence remainder (same pitfall dodged in AC4).
    assert "the world voice here." in html


# =========================================================================
# AC6 — World-tier files still render; no pack-sourced cultures section
# =========================================================================


@_live
def test_ac6_world_tier_renders_no_cultures_section(lore_html):
    """LORE_WORLD_FILES is unchanged, so world-tier files still render. But
    beneath_sunden authors NO cultures at the world tier, so after the cut no
    cultures section appears (it must NOT fall back to the pack's cultures)."""
    # World-tier files render (regression positive).
    assert 'id="file-history"' in lore_html, "world-tier history.yaml must still render"
    assert "was a working hold" in lore_html, "world lore.yaml must still render"
    # No cultures section — beneath_sunden has none at the world tier, and the
    # pack's cultures.yaml must no longer leak one in.
    assert 'id="file-cultures"' not in lore_html, (
        "beneath_sunden has no world-tier cultures; pack cultures must not leak a section"
    )


# =========================================================================
# AC7 — Dead constant removed; orthogonal spoiler allowlist untouched
# =========================================================================


def test_ac7_lore_pack_flavor_files_removed():
    """The flavor-merge is gone, so its driver constant has zero real consumers
    and must be deleted. Reflection tripwire (not a source grep) — refactor-
    stable per the No-Source-Text-Wiring-Tests rule."""
    assert not hasattr(reference_renderer, "LORE_PACK_FLAVOR_FILES"), (
        "LORE_PACK_FLAVOR_FILES must be deleted — absolute world-only leaves it dead code"
    )


def test_ac7_public_stems_unchanged():
    """``PUBLIC_STEMS`` in reference_visibility.py is an ORTHOGONAL spoiler
    allowlist (the line-81 reference to LORE_PACK_FLAVOR_FILES is only a
    comment). The flavor-file removal must not touch it — assert the flavor and
    world stems remain PUBLIC so a careless dead-code sweep can't yank them."""
    from sidequest.server.reference_visibility import PUBLIC_STEMS

    for stem in ("world", "cultures", "history", "lore", "locations", "factions"):
        assert stem in PUBLIC_STEMS, (
            f"PUBLIC_STEMS lost '{stem}' — it is an orthogonal spoiler allowlist, leave it intact"
        )
