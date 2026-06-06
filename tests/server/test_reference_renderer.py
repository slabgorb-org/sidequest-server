"""Unit tests for the reference-page YAML→HTML walker.

Renderer must produce stable, escaped HTML from arbitrary YAML trees. The walker
is pure (input dict/list/scalar → output str); no IO, no globals.
"""

from pathlib import Path

import pytest
import yaml as _yaml

from sidequest.server.reference_renderer import (
    EXCLUDED_FILES,
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
    # The id= anchor stays the raw slug; only the heading TEXT is humanized
    # (playtest 2026-05-27: raw snake_case keys leaked as headings).
    assert '<section id="name">' in html
    assert "<h2>Name</h2>" in html
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
    assert "<h2>Outer</h2>" in html
    assert '<section id="inner">' in html
    assert "<h2>Inner</h2>" in html
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
    # id= anchor stays the raw slug ("tier-1"); the heading humanizes the
    # id-derived display ("Tier 1") so a raw id never shows as a heading.
    assert '<section id="tier-1">' in html
    assert "<h3>Tier 1</h3>" in html
    assert '<section id="tier-two">' in html
    assert "<h3>Tier Two</h3>" in html
    assert '<section id="item-3">' in html
    assert "<h3>Item 3</h3>" in html


def test_snake_case_key_headings_are_humanized():
    """Regression (playtest 2026-05-27, road_warrior/the_circuit + caverns):
    the generic fallback dumped raw YAML keys as headings — the_maw,
    genre_conventions, rolls_per_slot, law_enforcement, one_percenters — onto
    player-facing Rules/Lore pages. They must render Title Case; the id=
    anchor must stay the raw slug for stable links.
    """
    html = render_node(
        {
            "the_maw": "x",
            "genre_conventions": "y",
            "rolls_per_slot": "z",
            "law_enforcement": "w",
        }
    )
    assert "<h2>The Maw</h2>" in html
    assert "<h2>Genre Conventions</h2>" in html
    assert "<h2>Rolls Per Slot</h2>" in html
    assert "<h2>Law Enforcement</h2>" in html
    # Anchors unchanged (stable deep-links).
    assert 'id="the-maw"' in html
    assert 'id="genre-conventions"' in html
    # No raw snake_case token survives in any heading.
    assert "<h2>the_maw</h2>" not in html
    assert "<h2>genre_conventions</h2>" not in html


def test_humanize_label_leaves_authored_prose_untouched():
    """Conservative guard: a heading that already has whitespace or any
    uppercase is author-formatted and must NOT be mangled (no "Floor It" →
    re-cased, no "Item 3" fallback altered, no acronym destroyed)."""
    from sidequest.server.reference_renderer import _humanize_label

    assert _humanize_label("Floor It") == "Floor It"  # already spaced
    assert _humanize_label("Item 3") == "Item 3"  # index fallback
    assert _humanize_label("McGuffin") == "McGuffin"  # proper noun w/ caps
    assert _humanize_label("USB") == "USB"  # acronym preserved
    assert _humanize_label("setting") == "Setting"  # bare lowercase word
    assert _humanize_label("floor_it") == "Floor It"  # snake_case id
    assert _humanize_label("tier-1") == "Tier 1"  # hyphen id
    assert _humanize_label("") == ""  # empty unchanged


def test_render_nested_dict_inside_list_recurses():
    html = render_node([{"name": "A", "stats": {"hp": 5, "atk": 2}}])
    assert "<h3>A</h3>" in html
    assert "<h2>Stats</h2>" in html
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
    # Anchor stays the raw slug; the id-derived heading humanizes.
    assert '<section id="fallback-id">' in html
    assert "<h3>Fallback Id</h3>" in html


def test_render_at_depth_cap_suppresses_raw_dump():
    # Build a 7-deep nested dict, one level past the cap (6). A player
    # reference page must NEVER emit a raw YAML/dict <pre> dump — the
    # over-cap subtree is suppressed (emits nothing) instead.
    deep = {"k": "leaf"}
    for _ in range(7):
        deep = {"k": deep}
    html = render_node(deep)
    assert "<pre>" not in html
    assert "</pre>" not in html


def test_depth_cap_suppression_emits_no_raw_yaml_text():
    # The deepest over-cap subtree must not leak its raw YAML re-dump
    # (e.g. "leaf: ..." / "g:") onto the page.
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "leaf"}}}}}}}
    html = render_node(deep)
    assert "<pre>" not in html
    assert "g:" not in html
    # Sanity check that the yaml alias is still importable.
    assert _yaml.safe_load("leaf: 1") == {"leaf": 1}


def test_below_depth_cap_renders_normally():
    nested = {"a": {"b": {"c": "deep_enough"}}}
    html = render_node(nested)
    assert "<pre>" not in html
    assert "<p>deep_enough</p>" in html


def test_rules_files_in_documented_order():
    # Story 63-5: tropes.yaml removed — keeper-side only per design bundle.
    assert RULES_FILES == (
        "archetypes.yaml",
        "classes.yaml",
        "rules.yaml",
        "progression.yaml",
        "magic.yaml",
        "power_tiers.yaml",
        "achievements.yaml",
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


def test_npcs_and_seed_tropes_are_excluded():
    assert "npcs.yaml" in EXCLUDED_FILES
    assert "seed_tropes.yaml" in EXCLUDED_FILES
    assert "prompts.yaml" in EXCLUDED_FILES


def test_tropes_yaml_excluded_from_rules_files():
    """Story 63-5 AC-7: tropes.yaml must NOT be in RULES_FILES.

    Tropes are keeper-side only per the design bundle. seed_tropes.yaml was
    already excluded; tropes.yaml was mistakenly left in RULES_FILES and
    rendered on rules reference pages.
    """
    assert "tropes.yaml" not in RULES_FILES
    assert "tropes.yaml" in EXCLUDED_FILES


def test_tropes_content_never_rendered_on_rules_page(tmp_path):
    """Story 63-5 AC-8/AC-9: fixture pack with tropes.yaml must NOT render
    trope content or section headings on the rules page.
    """
    from sidequest.server.reference_renderer import assemble_rules_page

    pack_dir = _write_pack(
        tmp_path,
        "demo",
        {
            "archetypes.yaml": "a: 1\n",
            "tropes.yaml": _yaml.safe_dump(
                {"tropes": [{"name": "Keeper Only Trope", "description": "secret"}]}
            ),
        },
    )
    html = assemble_rules_page("demo", pack_dir)
    assert "Keeper Only Trope" not in html
    assert "keeper only" not in html.lower()


def test_no_overlap_between_included_and_excluded():
    included = set(RULES_FILES) | set(LORE_WORLD_FILES)
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


def test_assemble_lore_page_renders_world_tier_only(tmp_path):
    """Story 63-10 (absolute world-only): the lore page renders world-tier
    files (``LORE_WORLD_FILES`` from ``world_dir``) and does NOT merge pack/
    genre-tier flavor. Replaces the prior ``…combines_world_and_pack_flavor``
    test, whose merge invariant was deliberately removed — a world's lore can
    contradict its pack's cosmology, so concatenating pack flavor produced
    incoherent pages (see ``test_reference_lore_world_only.py``).
    """
    from sidequest.server.reference_renderer import assemble_lore_page

    pack_dir = tmp_path / "demo"
    world_dir = pack_dir / "worlds" / "demoworld"
    world_dir.mkdir(parents=True)
    (pack_dir / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    # Pack-tier flavor — must NOT reach the page after the cut.
    (pack_dir / "lore.yaml").write_text("setting_anchor: genre-flavor-value\n")
    (pack_dir / "cultures.yaml").write_text(
        "- name: Genre Traveller\n  summary: a genre-culture value\n"
    )
    (world_dir / "world.yaml").write_text("description: Demoworld is a rainy procedural plateau.\n")
    (world_dir / "legends.yaml").write_text("- name: A Tale\n  summary: a tale\n")

    html = assemble_lore_page("demo", "demoworld", pack_dir, world_dir)

    # World-tier content renders.
    assert "<title>demo / demoworld — Lore</title>" in html
    assert "Demoworld" in html
    assert "a tale" in html
    assert 'id="file-world"' in html
    # Pack-tier flavor is NOT merged: neither its content nor a cultures section.
    assert "genre-flavor-value" not in html
    assert "genre-culture value" not in html
    assert 'id="file-cultures"' not in html


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


def test_glenross_time_precision_renders_without_raw_dict_text():
    """Regression: the live glenross calendar bug. A deeply-nested
    `time_precision` value (a dict-of-dict-of-dict deeper than the cap)
    must never dump a raw `{registers: {...}}` / `registers:` blob onto the
    player-facing page; the over-cap subtree is suppressed instead."""
    time_precision = {
        "registers": {
            "gentry": {"level": "minute", "example": "tea at four"},
            "staff": {"level": "hour", "example": "dawn chores"},
        }
    }
    # Wrap so `registers`/`level`/`example` cross the depth cap (6).
    node = {"time_precision": time_precision}
    for _ in range(6):
        node = {"calendar": node}
    html = render_node(node)
    # No raw dict-repr or YAML-dump text leaks.
    assert "<pre>" not in html
    assert "{'" not in html
    assert "registers:" not in html
    assert "{registers:" not in html
    assert "level: minute" not in html


def test_suppression_preserves_scalars_and_list_of_dicts():
    """The suppression path must only eat the raw-dump case. Ordinary
    scalars and shallow list-of-dicts (rendered as cards) still render."""
    node = {
        "tagline": "A quiet manor.",
        "people": [
            {"name": "Ada", "role": "butler"},
            {"name": "Boris", "role": "cook"},
        ],
    }
    html = render_node(node)
    assert "<p>A quiet manor.</p>" in html
    assert "<h3>Ada</h3>" in html
    assert "<p>butler</p>" in html
    assert "<h3>Boris</h3>" in html
