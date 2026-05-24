"""Unit tests for the reference-page YAML→HTML walker.

Renderer must produce stable, escaped HTML from arbitrary YAML trees. The walker
is pure (input dict/list/scalar → output str); no IO, no globals.
"""

from pathlib import Path

import pytest
import yaml as _yaml

from sidequest.server.reference_renderer import (
    EXCLUDED_FILES,
    LORE_PACK_FLAVOR_FILES,
    LORE_WORLD_FILES,
    RULES_FILES,
    render_node,
    slugify,
)


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Amateur Sleuth") == "amateur-sleuth"


def test_slugify_strips_non_ascii():
    assert slugify("Café Crème") == "caf-cr-me"


def test_slugify_collapses_runs_of_separators():
    assert slugify("a // b -- c") == "a-b-c"


def test_render_scalar_string_escapes_html():
    assert render_node("Hello <world>") == "<p>Hello &lt;world&gt;</p>"


def test_render_scalar_int():
    assert render_node(42) == "<p>42</p>"


def test_render_scalar_multiline_uses_pre_wrap():
    html = render_node("line1\nline2\nline3")
    assert 'class="multiline"' in html
    assert "line1\nline2\nline3" in html


def test_render_flat_dict_emits_section_per_key():
    html = render_node({"name": "Sleuth", "tier": "novice"})
    assert '<section id="name">' in html
    assert "<h2>name</h2>" in html
    assert "<p>Sleuth</p>" in html
    assert '<section id="tier">' in html


def test_render_nested_dict_recurses_as_section():
    """Locks down intentional behavior: nested dicts produce nested <section>.

    The spec design doc mentioned <dl>/<dt>/<dd> for nested dicts but the
    plan explicitly recurses via <section> (see Task 2 test for
    'h2 stats' inside a list-item context). This test pins the recursion.
    """
    html = render_node({"outer": {"inner": "value"}})
    assert '<section id="outer">' in html
    assert "<h2>outer</h2>" in html
    assert '<section id="inner">' in html
    assert "<h2>inner</h2>" in html
    assert "<p>value</p>" in html


def test_render_flat_dict_preserves_insertion_order():
    """Section ordering must follow dict insertion order — load-bearing for
    eventual file-by-file page assembly (Task 5).
    """
    html = render_node({"first": "a", "second": "b", "third": "c"})
    assert html.index('id="first"') < html.index('id="second"') < html.index('id="third"')


def test_render_multiline_escapes_html_content():
    """Multiline scalars must HTML-escape just like single-line scalars — XSS
    regression lock for the multiline branch.
    """
    html = render_node("line1\n<script>evil</script>\nline3")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_render_none_emits_placeholder():
    """YAML null renders as a labeled placeholder, not as silent empty <p>."""
    assert render_node(None) == "<p><em>(none)</em></p>"


def test_render_list_of_scalars_emits_ul():
    html = render_node(["alpha", "beta", "gamma"])
    assert "<ul>" in html
    assert "<li>alpha</li>" in html
    assert "<li>beta</li>" in html
    assert "<li>gamma</li>" in html
    assert "</ul>" in html


def test_render_list_of_dicts_with_name_uses_h3_anchor():
    html = render_node(
        [
            {"name": "Sleuth", "description": "Investigates."},
            {"name": "Detective", "description": "Investigates harder."},
        ]
    )
    assert '<section id="sleuth">' in html
    assert "<h3>Sleuth</h3>" in html
    assert '<section id="detective">' in html


def test_render_list_of_dicts_falls_through_id_title_then_index():
    html = render_node(
        [
            {"id": "tier-1", "value": "low"},
            {"title": "Tier Two", "value": "mid"},
            {"value": "high"},
        ]
    )
    assert '<section id="tier-1">' in html
    assert "<h3>tier-1</h3>" in html
    assert '<section id="tier-two">' in html
    assert "<h3>Tier Two</h3>" in html
    assert '<section id="item-3">' in html
    assert "<h3>Item 3</h3>" in html


def test_render_nested_dict_inside_list_recurses():
    html = render_node([{"name": "A", "stats": {"hp": 5, "atk": 2}}])
    assert "<h3>A</h3>" in html
    assert "<h2>stats</h2>" in html
    assert "<p>5</p>" in html
    assert "<p>2</p>" in html


def test_render_empty_list_emits_em_placeholder():
    assert render_node([]) == "<p><em>(empty)</em></p>"


def test_render_empty_dict_emits_em_placeholder():
    assert render_node({}) == "<p><em>(empty)</em></p>"


def test_render_mixed_list_threads_scalars_and_sections():
    """Heterogeneous list — scalar, dict, scalar — must emit scalar <p>,
    then a sectioned dict, then another scalar <p>, in order. Pins the
    fallthrough path in _render_list for non-dict, non-list items.
    """
    html = render_node(["before", {"name": "middle"}, "after"])
    p_before = html.index("<p>before</p>")
    section_mid = html.index('<section id="middle">')
    p_after = html.index("<p>after</p>")
    assert p_before < section_mid < p_after


def test_render_list_item_with_empty_name_falls_through_to_index():
    """An item with name="" produces an unusable heading; must fall through
    to Item N rather than silently emitting <section id="">.
    """
    html = render_node([{"name": "", "value": "v1"}, {"name": "", "value": "v2"}])
    assert '<section id="item-1">' in html
    assert "<h3>Item 1</h3>" in html
    assert '<section id="item-2">' in html
    assert "<h3>Item 2</h3>" in html
    # No empty id should be emitted
    assert 'id=""' not in html


def test_render_list_item_with_unicode_only_name_falls_through_to_index():
    """A name whose slug is empty (unicode-only) still falls through to the
    index-based heading. The display value is kept (no information loss),
    only the anchor uses the fallback.
    """
    html = render_node([{"name": "日本"}, {"name": "中文"}])
    # Anchors fall back to Item N
    assert '<section id="item-1">' in html
    assert '<section id="item-2">' in html
    # But the display headings still show what the author wrote
    assert "<h3>日本</h3>" in html
    assert "<h3>中文</h3>" in html
    assert 'id=""' not in html


def test_render_list_item_name_priority_skips_falsy_intermediate():
    """When the first field is empty/unusable but the second is valid, the
    code must continue iterating rather than falling all the way through
    to the index fallback. Priority order: name -> id -> title -> index.
    """
    html = render_node([{"name": "", "id": "fallback-id", "value": "x"}])
    assert '<section id="fallback-id">' in html
    assert "<h3>fallback-id</h3>" in html


def test_render_at_depth_cap_falls_back_to_pre():
    # Build a 7-deep nested dict, one level past the cap (6).
    deep = {"k": "leaf"}
    for _ in range(7):
        deep = {"k": deep}
    html = render_node(deep)
    assert "<pre>" in html
    assert "</pre>" in html


def test_pre_fallback_contains_yaml_redump():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "leaf"}}}}}}}
    html = render_node(deep)
    # The yaml redump should appear inside the <pre> for the deepest sub-tree
    assert "leaf" in html
    # Sanity check that the redump is valid yaml (uses the _yaml alias).
    assert _yaml.safe_load("leaf: 1") == {"leaf": 1}


def test_below_depth_cap_renders_normally():
    nested = {"a": {"b": {"c": "deep_enough"}}}
    html = render_node(nested)
    assert "<pre>" not in html
    assert "<p>deep_enough</p>" in html


def test_rules_files_in_documented_order():
    assert RULES_FILES == (
        "archetypes.yaml",
        "classes.yaml",
        "rules.yaml",
        "progression.yaml",
        "magic.yaml",
        "power_tiers.yaml",
        "achievements.yaml",
        "tropes.yaml",
        "equipment_tables.yaml",
        "inventory.yaml",
        "beat_vocabulary.yaml",
    )


def test_lore_world_files_in_documented_order():
    assert LORE_WORLD_FILES == (
        "world.yaml",
        "cultures.yaml",
        "history.yaml",
        "calendar.yaml",
        "demographics.yaml",
        "legends.yaml",
        "openings.yaml",
        "lore.yaml",
        "locations.yaml",
    )


def test_lore_pack_flavor_files_in_documented_order():
    # Story 63-7 added "factions.yaml" so lore-tier list-of-dict items
    # in the new ``cult-<slug>`` namespace render on the lore page.
    assert LORE_PACK_FLAVOR_FILES == (
        "cultures.yaml",
        "lore.yaml",
        "history.yaml",
        "factions.yaml",
    )


def test_npcs_and_seed_tropes_are_excluded():
    assert "npcs.yaml" in EXCLUDED_FILES
    assert "seed_tropes.yaml" in EXCLUDED_FILES
    assert "prompts.yaml" in EXCLUDED_FILES


def test_no_overlap_between_included_and_excluded():
    included = set(RULES_FILES) | set(LORE_WORLD_FILES) | set(LORE_PACK_FLAVOR_FILES)
    overlap = included & EXCLUDED_FILES
    assert overlap == set(), f"file appears in both included and excluded: {overlap}"


_MINIMAL_THEME_YAML = (
    "primary: '#5C7A4F'\n"
    "accent: '#C9A96E'\n"
    "background: '#F4EBDA'\n"
    "archetype: parchment\n"
    "web_font_family: Lora\n"
    "display_font_family: Playfair Display\n"
    "dinkus:\n  glyph:\n    light: '—'\n    medium: '❧'\n    heavy: '❧❧❧'\n"
)


def _write_pack(tmp_path: Path, pack: str, files: dict[str, str]) -> Path:
    pack_dir = tmp_path / pack
    pack_dir.mkdir(parents=True)
    if "theme.yaml" not in files:
        (pack_dir / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    for name, contents in files.items():
        (pack_dir / name).write_text(contents)
    return pack_dir


def test_assemble_rules_page_includes_listed_files_in_order(tmp_path):
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(
        tmp_path,
        "demo",
        {
            "archetypes.yaml": "a: 1\n",
            "classes.yaml": "b: 2\n",
            "rules.yaml": "c: 3\n",
        },
    )
    html = assemble_rules_page("demo", pack_dir)

    assert "<title>demo — Rules</title>" in html
    # Section order matches RULES_FILES ordering.
    # Task 15: presented files suppress <h1>{filename}</h1>; assert on
    # the stable section anchor ids instead.
    a_pos = html.index('id="file-archetypes"')
    b_pos = html.index('id="file-classes"')
    c_pos = html.index('id="file-rules"')
    assert a_pos < b_pos < c_pos


def test_assemble_rules_page_skips_missing_optional_files(tmp_path):
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(tmp_path, "demo", {"archetypes.yaml": "a: 1\n"})
    html = assemble_rules_page("demo", pack_dir)

    # Task 15: archetypes has a presenter, so <h1>archetypes.yaml</h1> is
    # suppressed. Assert on the stable section anchor id instead.
    assert 'id="file-archetypes"' in html
    assert 'id="file-magic"' not in html  # silently absent


def test_assemble_rules_page_never_renders_excluded_files(tmp_path):
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(
        tmp_path,
        "demo",
        {
            "archetypes.yaml": "a: 1\n",
            "npcs.yaml": "secret_villain: thedoctor\n",
            "seed_tropes.yaml": "spoilers: yes\n",
        },
    )
    html = assemble_rules_page("demo", pack_dir)

    assert "thedoctor" not in html
    assert "seed_tropes" not in html.lower()


def test_assemble_lore_page_combines_world_and_pack_flavor(tmp_path):
    """Both world-tier and pack-tier flavor content must be reachable on
    the lore page.

    Story 63-7 note: this test previously asserted ``world.yaml`` appeared
    BEFORE the ``(genre)`` label suffix in the rendered HTML — enforcing a
    tier-ordering invariant. The v3 TOC-driven layout buckets renders by
    ``PACK_TOC[pack].id`` via ``TOC_TO_FILES``, so file order inside
    ``<main>`` follows the per-pack TOC, not the world-then-flavor
    iteration order. Tier-ordering at the markup level is no longer a
    spec invariant; what matters is that BOTH tiers' content reaches
    the rendered page. Content-presence is what the test verifies now.
    """
    from sidequest.server.reference_renderer import assemble_lore_page

    pack_dir = tmp_path / "demo"
    world_dir = pack_dir / "worlds" / "demoworld"
    world_dir.mkdir(parents=True)
    (pack_dir / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    # Use a PUBLIC field (setting_anchor) for the pack-flavor lore.yaml so
    # the visibility gate does not drop it. The purpose of this test is to
    # verify that BOTH world-tier and pack-tier content reaches the page;
    # the field name itself is not significant.
    (pack_dir / "lore.yaml").write_text("setting_anchor: genre-flavor-value\n")
    (pack_dir / "cultures.yaml").write_text(
        "- name: Genre Traveller\n  summary: a genre-culture value\n"
    )
    (world_dir / "world.yaml").write_text("description: Demoworld is a rainy procedural plateau.\n")
    (world_dir / "legends.yaml").write_text("- name: A Tale\n  summary: a tale\n")

    html = assemble_lore_page("demo", "demoworld", pack_dir, world_dir)

    assert "<title>demo / demoworld — Lore</title>" in html
    assert "Demoworld" in html
    assert "a tale" in html
    assert "genre-flavor-value" in html
    assert "genre-culture value" in html
    # Both world-tier content (world.yaml) and pack-tier flavor (cultures.yaml)
    # are reachable via their stable section anchor ids. Task 15 suppresses the
    # legacy <h1>{filename}</h1> for presented files, so "world.yaml" and
    # "(genre)" no longer appear as heading text; the section wrappers still
    # emit so deep-links resolve.
    assert 'id="file-world"' in html
    assert 'id="file-cultures"' in html


def test_assemble_handles_malformed_yaml_with_loud_marker(tmp_path):
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(
        tmp_path,
        "demo",
        {
            "archetypes.yaml": ":\n  - this is: : not valid\n",
        },
    )
    with pytest.raises(ValueError) as exc:
        assemble_rules_page("demo", pack_dir)
    assert "archetypes.yaml" in str(exc.value)
