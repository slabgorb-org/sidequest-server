"""Tests for hero section and contents-rail chrome (Tasks 21 + 22 / AC5–AC10).

Hero displays world name + lore epigraph from lore.yaml; falls back to pack
name with a WARN-level span when the world is unbound. Contents rail is a
locked TOC with IntersectionObserver scroll-spy hooks. Per-file section
wrappers carry stable ids the TOC links to.

Fixtures are tmp-path packs — never live ``genre_packs/*``.
"""

from __future__ import annotations

from pathlib import Path

# --- Fixture helpers ---


_THEME_YAML = (
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


def _seed_pack(tmp_path: Path) -> Path:
    pack = tmp_path / "demo"
    pack.mkdir(parents=True)
    (pack / "theme.yaml").write_text(_THEME_YAML)
    (pack / "archetypes.yaml").write_text("kinds:\n  - sleuth\n")
    (pack / "classes.yaml").write_text("- name: knight\n  signature: charge\n")
    (pack / "cultures.yaml").write_text("- name: highlander\n  language: gaelic\n")
    return pack


def _seed_world(pack_dir: Path, world_name: str, *, with_lore: bool = True) -> Path:
    world = pack_dir / "worlds" / world_name
    world.mkdir(parents=True)
    (world / "world.yaml").write_text(f"name: {world_name.title()}\n")
    if with_lore:
        (world / "lore.yaml").write_text(
            f"world_name: {world_name.title()}\n"
            f"epigraph: A quiet valley where nothing has happened for a hundred years.\n"
        )
    (world / "legends.yaml").write_text("- name: the-grey-pilgrim\n  origin: unknown\n")
    return world


# --- Hero section (Task 21 / AC5+AC6) ---


def test_lore_page_emits_hero_with_world_name(tmp_path):
    """Hero block displays the world name from lore.yaml."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack, "glenross")

    html = assemble_lore_page("demo", "glenross", pack, world)

    assert "Glenross" in html
    # Hero is structurally a header/section, not just any mention of the name
    assert 'class="hero"' in html or "<header" in html


def test_lore_page_hero_includes_epigraph(tmp_path):
    """Hero block contains the lore.yaml epigraph text.

    Scoped to the hero region so a regression that breaks the hero but still
    renders the epigraph elsewhere (e.g. via lore.yaml's normal file section)
    fails this test."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack, "glenross")

    html = assemble_lore_page("demo", "glenross", pack, world)

    # Anchor on hero id, then look forward to the first closing tag that ends
    # the hero block. ValueError on missing id="hero" is the RED-phase signal.
    hero_start = html.index('id="hero"')
    # Take a generous window past the hero opener — the epigraph must sit inside it.
    hero_window = html[hero_start : hero_start + 2000]
    # The end of the hero block ends before the first file section.
    file_section_pos = hero_window.find('class="file"')
    if file_section_pos != -1:
        hero_window = hero_window[:file_section_pos]
    assert "quiet valley" in hero_window


def test_lore_page_hero_has_stable_id(tmp_path):
    """Hero block has a stable id so the contents rail can link to it and the
    scroll-spy can detect it."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack, "glenross")

    html = assemble_lore_page("demo", "glenross", pack, world)

    assert 'id="hero"' in html


def test_lore_page_hero_falls_back_when_lore_missing(tmp_path):
    """No lore.yaml present → hero falls back to pack name. WARN, not silent."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack, "glenross", with_lore=False)

    html = assemble_lore_page("demo", "glenross", pack, world)

    # Hero still emitted, falling back to pack name
    assert 'id="hero"' in html
    assert "demo" in html


def test_lore_page_hero_escapes_special_chars(tmp_path):
    """XSS guard: world name inside the hero must be HTML-escaped.

    Scoped to the hero region — escaping in the lore.yaml file section
    (which already works today via _render_scalar) does NOT satisfy this
    test. The hero is a separate code path; it must escape independently."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = pack / "worlds" / "evil"
    world.mkdir(parents=True)
    (world / "lore.yaml").write_text(
        'world_name: "<script>alert(1)</script>"\n'
        "epigraph: harmless\n"
    )

    html = assemble_lore_page("demo", "evil", pack, world)

    # Locate the hero block; ValueError on missing id="hero" is RED signal.
    hero_start = html.index('id="hero"')
    hero_window = html[hero_start : hero_start + 2000]
    file_section_pos = hero_window.find('class="file"')
    if file_section_pos != -1:
        hero_window = hero_window[:file_section_pos]
    # Raw script tag MUST NOT appear in the hero
    assert "<script>alert(1)</script>" not in hero_window
    # Escaped form MUST appear in the hero
    assert "&lt;script&gt;" in hero_window


def test_lore_page_hero_precedes_file_sections(tmp_path):
    """Hero block must appear in the document BEFORE the per-file sections so
    it renders first in the visual flow."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack, "glenross")

    html = assemble_lore_page("demo", "glenross", pack, world)

    hero_pos = html.index('id="hero"')
    first_section_pos = html.index('class="file"')
    assert hero_pos < first_section_pos


# --- Contents rail (Task 22 / AC7–AC10) ---


def test_lore_page_emits_contents_rail(tmp_path):
    """Lore page renders a contents-rail navigation block."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack, "glenross")

    html = assemble_lore_page("demo", "glenross", pack, world)

    # Accept either <nav class="contents-rail"> or <aside class="toc">
    assert 'class="contents-rail"' in html or 'class="toc"' in html


def test_rules_page_emits_contents_rail(tmp_path):
    """Rules page also gets a contents rail."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("demo", pack)

    assert 'class="contents-rail"' in html or 'class="toc"' in html


def test_contents_rail_links_to_per_file_section_ids(tmp_path):
    """TOC entries must reference the same `file-{stem}` ids that section
    wrappers use, so click-to-jump and scroll-spy work."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("demo", pack)

    assert "#file-archetypes" in html
    assert "#file-classes" in html
    # And the corresponding section wrappers exist
    assert 'id="file-archetypes"' in html
    assert 'id="file-classes"' in html


def test_contents_rail_has_scroll_spy_hooks(tmp_path):
    """Contents rail markup includes data-scroll-spy hooks (or equivalent) so
    the inline IntersectionObserver script can attach to it."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("demo", pack)

    assert "data-scroll-spy" in html or "data-spy" in html


def test_contents_rail_is_locked_no_toggle(tmp_path):
    """Contents rail is locked open — no toggle button, no aria-expanded, no collapse."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("demo", pack)

    # The rail is present...
    assert 'class="contents-rail"' in html or 'class="toc"' in html
    # ...but no toggle UI
    assert "toc-toggle" not in html
    assert "contents-toggle" not in html
    # aria-expanded would only appear on a collapsible nav
    assert "aria-expanded" not in html


def test_contents_rail_includes_hero_link_on_lore_page(tmp_path):
    """Lore page TOC has an entry that targets the hero anchor."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack, "glenross")

    html = assemble_lore_page("demo", "glenross", pack, world)

    assert "#hero" in html


# --- Inline scroll-spy JS ---


def test_inline_scroll_spy_js_present(tmp_path):
    """An inline <script> block implements the IntersectionObserver scroll-spy.
    Per the spec, ~15 LOC, no external bundle."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("demo", pack)

    assert "IntersectionObserver" in html


def test_inline_scroll_spy_js_is_bounded(tmp_path):
    """The spec budgets ~15 lines for the scroll-spy. Allow a generous ceiling
    (30 lines / 2KB) to catch accidental SPA-bundle inlining."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("demo", pack)

    # Locate the scroll-spy script block by its IntersectionObserver marker.
    io_pos = html.index("IntersectionObserver")
    # Walk backward to the nearest <script> opener.
    script_open = html.rfind("<script", 0, io_pos)
    script_close = html.index("</script>", io_pos)
    assert script_open != -1
    script_body = html[script_open:script_close]
    # Hard ceiling so a future inlined SPA bundle would trip the test.
    assert len(script_body) < 2048, f"scroll-spy <script> too large: {len(script_body)} bytes"


def test_no_external_scroll_spy_bundle(tmp_path):
    """No <script src="..."> pointing to a scroll-spy or SPA bundle. Inline only."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("demo", pack)

    assert 'src="/static/scroll' not in html
    assert "scroll-spy.js" not in html


# --- Per-file section wrappers (Task 22 / AC8) ---


def test_per_file_section_wrappers_have_stable_ids(tmp_path):
    """Each rendered file is wrapped in `<section class="file" id="file-{stem}">`."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("demo", pack)

    assert 'id="file-archetypes"' in html
    assert 'id="file-classes"' in html


def test_listdict_item_ids_remain_namespaced(tmp_path):
    """Regression guard for Task 2: knight under classes.yaml → `class-knight`,
    highlander under cultures.yaml → `culture-highlander`."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack, "glenross")

    html = assemble_lore_page("demo", "glenross", pack, world)

    # classes.yaml emits via rules page, cultures via lore (pack flavor)
    assert 'id="culture-highlander"' in html


def test_listdict_item_ids_namespaced_in_rules_page(tmp_path):
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("demo", pack)

    assert 'id="class-knight"' in html
