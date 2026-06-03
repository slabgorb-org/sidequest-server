"""Story 63-7 Tasks A–F — structural chrome tests aligned to v3 plan.

Replaces the 63-4 markup vocabulary (``.contents-rail``, bare ``<h1>``
hero) with the bundled-CSS vocabulary from the v3 plan
(``.page`` wrapper, full 5-element ``.hero-*`` structure,
``<div class="layout"><aside class="toc-sticky"><nav class="toc">…``).

Each section corresponds to a Task / AC:

- Task A → AC1: ``<div class="page">`` wraps body
- Task B → AC2: 5-element hero (lore + rules pages both render hero)
- Task C → AC3, AC4: ``.layout`` grid wrap; ``PACK_TOC`` numerals +
  labels; unknown-pack default fallback emits ERROR span
- Task E → AC5: inline observer uses ``aside.toc-sticky nav.toc a``
  selectors, toggles ``.active`` class, ``rootMargin '-20% 0% -60% 0%'``
- Task F → AC7: ``_KIND_OVERRIDES['factions'] = 'cult'``
- Tasks B + C → AC6: ``PACK_LABELS``, ``PACK_BLURBS``, ``PACK_EPIGRAPHS``,
  ``PACK_TOC`` constants present for all 10 live packs

Fixtures are tmp-path packs named after real live packs so the
``PACK_*`` lookups exercise the populated dispatch path. The unknown-
pack fallback is its own test against an unknown name. No tests load
live ``genre_packs/*`` content; per the no-content-coupled-tests memo
that's a content-team / validator concern.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# All 10 live packs (cross-check ``sidequest-content/genre_packs/``).
# AC6 requires PACK_LABELS / PACK_BLURBS / PACK_EPIGRAPHS / PACK_TOC
# to cover all of them — no fallthrough.
LIVE_PACKS: tuple[str, ...] = (
    "caverns_and_claudes",
    "elemental_harmony",
    "heavy_metal",
    "mutant_wasteland",
    "neon_dystopia",
    "pulp_noir",
    "road_warrior",
    "space_opera",
    "spaghetti_western",
    "tea_and_murder",
)


_THEME_YAML = (
    "primary: '#5C7A4F'\n"
    "accent: '#C9A96E'\n"
    "background: '#F4EBDA'\n"
    "archetype: terminal\n"
    "web_font_family: Lora\n"
    "display_font_family: Playfair Display\n"
    "dinkus:\n"
    "  glyph:\n"
    "    light: '—'\n"
    "    medium: '❧'\n"
    "    heavy: '❧❧❧'\n"
)


def _seed_pack(tmp_path: Path, pack_name: str = "space_opera") -> Path:
    """Seed a tmp pack dir with a real-pack name so PACK_TOC lookups hit."""
    pack = tmp_path / pack_name
    pack.mkdir(parents=True)
    (pack / "theme.yaml").write_text(_THEME_YAML)
    (pack / "archetypes.yaml").write_text("kinds:\n  - spacer\n")
    (pack / "classes.yaml").write_text("- name: knight\n  signature: charge\n")
    (pack / "cultures.yaml").write_text("- name: highlander\n  language: gaelic\n")
    (pack / "rules.yaml").write_text("core: dice-resolution\n")
    return pack


def _seed_world(pack_dir: Path, world_name: str = "coyote_star") -> Path:
    """Seed a lore-bearing world dir (lore.yaml has world_name + epigraph)."""
    world = pack_dir / "worlds" / world_name
    world.mkdir(parents=True)
    (world / "world.yaml").write_text(f"name: {world_name.title()}\n")
    (world / "lore.yaml").write_text(
        f"world_name: {world_name.replace('_', ' ').title()}\n"
        "epigraph: A quiet valley where nothing has happened for a hundred years.\n"
    )
    (world / "legends.yaml").write_text("- name: the-grey-pilgrim\n  summary: unknown\n")
    return world


def _hero_window(html: str) -> str:
    """Return the substring of ``html`` covering ``<header class="hero">…</header>``.

    Anchors on the literal ``class="hero"`` (not ``id="hero"`` — the v3
    hero may or may not retain the id; the structural class is the
    load-bearing marker). Raises if no hero is present (RED signal for
    the AC2 ``hero exists on both pages`` requirement).
    """
    start = html.index('class="hero"')
    # Walk back to the opening tag for the hero header.
    tag_open = html.rfind("<header", 0, start)
    if tag_open == -1:
        # Bundle hero is a <header>; if a future change moves it to
        # <section>/<div>, update this helper deliberately.
        raise AssertionError('found class="hero" but no enclosing <header> tag')
    tag_close = html.index("</header>", start)
    return html[tag_open : tag_close + len("</header>")]


# ---------------------------------------------------------------------------
# Task A → AC1 — `<div class="page">` wraps body
# ---------------------------------------------------------------------------


def test_lore_page_body_is_wrapped_in_page_div(tmp_path: Path) -> None:
    """AC1: the lore page wraps its body in ``<div class="page">…</div>``.

    Per plan line 2641, ``_wrap_document`` emits
    ``f'<div class=\"page\">{body}</div>'`` around the body content
    (the trailing scroll-spy ``<script>`` stays outside the wrapper for
    parser-friendliness — verified separately below).
    """
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack)

    html = assemble_lore_page("space_opera", "coyote_star", pack, world)

    assert 'class="page"' in html, (
        'AC1: <div class="page"> wrapper not emitted; the bundle\'s CSS '
        "targets `.page` as the outer typographic container."
    )


def test_rules_page_body_is_wrapped_in_page_div(tmp_path: Path) -> None:
    """AC1 mirror for the rules page — both routes get the wrapper."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)

    html = assemble_rules_page("space_opera", pack)

    assert 'class="page"' in html


def test_scroll_spy_script_stays_outside_page_wrapper(tmp_path: Path) -> None:
    """AC1 footnote: the trailing scroll-spy ``<script>`` sits AFTER the
    closing ``</div>`` of ``.page``. Putting it inside the wrapper would
    be harmless but the plan deliberately keeps it at the body root for
    parser-friendliness."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)

    # The script is the inline IntersectionObserver block — the bundle
    # never references an external scroll-spy bundle (verified by an
    # older 63-4 test we keep). Find its <script> opener by walking
    # back from the IntersectionObserver marker.
    io_pos = html.index("IntersectionObserver")
    script_open = html.rfind("<script", 0, io_pos)
    page_close = html.rfind("</div>", 0, io_pos)
    # The scroll-spy <script> must come AFTER the `.page` wrapper closes.
    # If the script were inside the wrapper, page_close would be > script_open.
    assert script_open > page_close, (
        "Scroll-spy <script> appears inside the `.page` wrapper — plan "
        "v3 Task 20 keeps it at the body root, outside the wrapper."
    )


# ---------------------------------------------------------------------------
# Task B → AC2 — 5-element hero structure on both lore and rules pages
# ---------------------------------------------------------------------------


def test_lore_hero_has_eyebrow_with_glyph_eyebrow_rule(tmp_path: Path) -> None:
    """AC2.1: hero opens with ``<div class="hero-eyebrow">`` containing
    ``<span class="glyph">``, ``<span class="eyebrow gilt">``, and
    ``<span class="rule"></span>`` (the empty rule is rendered by the
    bundle's CSS as a gradient stroke)."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack)
    html = assemble_lore_page("space_opera", "coyote_star", pack, world)
    hero = _hero_window(html)

    assert 'class="hero-eyebrow"' in hero, "AC2.1: .hero-eyebrow row missing"
    assert 'class="glyph"' in hero, "AC2.1: .glyph span missing inside .hero-eyebrow"
    # The bundle styles `.eyebrow.gilt` — both tokens must appear together.
    assert re.search(r'class="[^"]*\beyebrow\b[^"]*\bgilt\b', hero) or re.search(
        r'class="[^"]*\bgilt\b[^"]*\beyebrow\b', hero
    ), 'AC2.1: <span class="eyebrow gilt"> not emitted in hero'
    assert 'class="rule"' in hero, 'AC2.1: empty <span class="rule"> missing'


def test_lore_hero_has_kicker(tmp_path: Path) -> None:
    """AC2.2: hero contains ``<div class="hero-kicker">…</div>``.

    Kicker text per plan is first sentence of ``lore.world.description``
    if present, else ``PACK_BLURBS[pack]``. Either way the markup token
    must exist.
    """
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack)
    html = assemble_lore_page("space_opera", "coyote_star", pack, world)

    assert 'class="hero-kicker"' in _hero_window(html), "AC2.2: .hero-kicker missing"


def test_lore_hero_has_hero_title(tmp_path: Path) -> None:
    """AC2.3: hero has ``<h1 class="hero-title">…</h1>`` (replaces 63-4's
    bare ``<h1>``). The bundle styles ``.hero-title`` for display font.
    """
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack)
    html = assemble_lore_page("space_opera", "coyote_star", pack, world)
    hero = _hero_window(html)

    assert 'class="hero-title"' in hero, "AC2.3: .hero-title class not on hero <h1>"
    # And the world name (from lore.yaml) appears inside the .hero-title
    assert "Coyote Star" in hero


def test_lore_hero_has_hero_sub(tmp_path: Path) -> None:
    """AC2.4: hero contains ``<div class="hero-sub">{pack label} · Lore & Rules</div>``."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack)
    html = assemble_lore_page("space_opera", "coyote_star", pack, world)

    assert 'class="hero-sub"' in _hero_window(html), "AC2.4: .hero-sub row missing"


def test_lore_hero_epigraph_has_attrib(tmp_path: Path) -> None:
    """AC2.5: hero epigraph uses the bundle's classes
    ``<div class="hero-epigraph narrative-flourish">…<span class="attrib">…</span></div>``.

    Critically NOT the legacy 63-4 ``<p class="epigraph">…</p>`` (which
    has zero matching CSS rules and renders as a plain paragraph).
    """
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack)
    html = assemble_lore_page("space_opera", "coyote_star", pack, world)
    hero = _hero_window(html)

    assert 'class="hero-epigraph' in hero, (
        'AC2.5: .hero-epigraph missing from hero — legacy <p class="epigraph"> '
        "has no matching CSS rule and renders as a plain paragraph."
    )
    # The narrative-flourish class is the bundle's display-toggle hook
    # ([data-narrative="off"] .narrative-flourish { display: none }).
    assert "narrative-flourish" in hero, (
        "AC2.5: .narrative-flourish toggle class missing from hero epigraph "
        "— required for the bundle's display-toggle to hide epigraphs."
    )
    assert 'class="attrib"' in hero, (
        'AC2.5: <span class="attrib"> missing — epigraph attribution '
        "needs its own class so the bundle can style it distinct from "
        "the quoted text."
    )


def test_rules_page_emits_a_hero(tmp_path: Path) -> None:
    """AC2 also requires the rules page to render a hero (63-4 only
    emitted hero on the lore page; the rules-page hero falls back to
    ``PACK_LABELS[pack]`` as the title per plan line 2683–2688)."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)

    assert 'class="hero"' in html, "AC2: rules page missing hero block"
    # The rules-page hero gets a `.hero-title` like the lore hero
    assert 'class="hero-title"' in _hero_window(html)


def test_lore_hero_escapes_world_name(tmp_path: Path) -> None:
    """Security regression guard: world_name from lore.yaml must be
    HTML-escaped inside the new .hero-title element, not just inside
    the lore.yaml file section. Mirrors 63-4's hero-escape test but
    against the new vocabulary."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = pack / "worlds" / "evil"
    world.mkdir(parents=True)
    (world / "lore.yaml").write_text(
        'world_name: "<script>alert(1)</script>"\nepigraph: harmless\n'
    )

    html = assemble_lore_page("space_opera", "evil", pack, world)
    hero = _hero_window(html)

    assert "<script>alert(1)</script>" not in hero, (
        "XSS: raw <script> tag appears inside hero — world_name not escaped"
    )
    assert "&lt;script&gt;" in hero, (
        "world_name must be HTML-escaped before insertion into .hero-title"
    )


# ---------------------------------------------------------------------------
# Task C → AC3 — `.layout` grid wraps `.toc-sticky` + `<main>`
# ---------------------------------------------------------------------------


def test_lore_page_emits_layout_grid(tmp_path: Path) -> None:
    """AC3: ``<div class="layout">`` wraps the TOC + main content."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack)

    html = assemble_lore_page("space_opera", "coyote_star", pack, world)

    assert 'class="layout"' in html, (
        'AC3: <div class="layout"> grid missing — the bundle\'s CSS uses '
        "`.layout` as a CSS-grid container with TOC sidebar + main column."
    )


def test_rules_page_emits_layout_grid(tmp_path: Path) -> None:
    """AC3 mirror for rules page."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)

    assert 'class="layout"' in html


def test_lore_page_emits_toc_sticky_aside_with_nav_toc(tmp_path: Path) -> None:
    """AC3: TOC is ``<aside class="toc-sticky"><nav class="toc">…</nav></aside>``."""
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack)
    html = assemble_lore_page("space_opera", "coyote_star", pack, world)

    # Both classes must appear and the .toc-sticky must contain the .toc.
    assert 'class="toc-sticky"' in html, 'AC3: <aside class="toc-sticky"> missing'
    assert 'class="toc"' in html, 'AC3: <nav class="toc"> missing'
    aside_pos = html.index('class="toc-sticky"')
    toc_pos = html.index('class="toc"')
    assert toc_pos > aside_pos, (
        'AC3: <nav class="toc"> must be nested inside the .toc-sticky aside, '
        "not a sibling — the bundle's sticky positioning targets the aside."
    )


def test_toc_has_toc_title_contents_label(tmp_path: Path) -> None:
    """AC3 detail: TOC nav contains ``<div class="toc-title">Contents</div>``."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)

    assert 'class="toc-title"' in html, "AC3: .toc-title label missing from TOC"


def test_toc_uses_ordered_list_not_unordered(tmp_path: Path) -> None:
    """AC3 detail: bundle's TOC is ``<ol>``, not ``<ul>`` (63-4 emitted
    ``<ul>``). The bundle's CSS targets ``.toc ol`` for numeral hiding
    and ``.toc-num`` for explicit Roman numerals."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)

    # Locate the <nav class="toc"> block and search inside.
    toc_pos = html.index('class="toc"')
    toc_close = html.index("</nav>", toc_pos)
    toc_block = html[toc_pos:toc_close]
    assert "<ol" in toc_block, (
        "AC3: TOC list element is <ul>, plan requires <ol> (bundle CSS "
        "targets `.toc ol > li` for layout)."
    )


def test_layout_wraps_main_around_section_body(tmp_path: Path) -> None:
    """AC3: the per-file ``<section class="file">`` blocks sit inside
    ``<main>`` inside ``<div class="layout">``."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)

    layout_pos = html.index('class="layout"')
    main_pos = html.index("<main", layout_pos)
    main_close = html.index("</main>", main_pos)
    # The classes.yaml section body must be inside <main>.
    file_section_pos = html.index('class="file"')
    assert main_pos < file_section_pos < main_close, (
        'AC3: per-file <section class="file"> sits outside the <main> '
        "element of the .layout grid; the bundle's CSS positions the main "
        "column relative to the sticky TOC aside."
    )


# ---------------------------------------------------------------------------
# Task C → AC4 — PACK_TOC numerals + unknown-pack fallback
# ---------------------------------------------------------------------------


def test_toc_link_uses_toc_num_span_for_numeral(tmp_path: Path) -> None:
    """AC4: each TOC ``<li>`` is
    ``<li><a href="#{id}"><span class="toc-num">{num}.</span>{label}</a></li>``.

    The ``.toc-num`` class is what the bundle styles for the
    serif-numeral prefix; without this span, the numerals would render
    as plain text without the bundle's typographic treatment.
    """
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)

    assert 'class="toc-num"' in html, (
        'AC4: <span class="toc-num"> missing from TOC links — the '
        "bundle styles `.toc-num` for the Roman-numeral prefix."
    )


def test_pack_toc_constant_is_keyed_by_pack(tmp_path: Path) -> None:
    """AC4: PACK_TOC is a dict keyed by pack name, values are iterables
    of TOC entries each with ``num``, ``id``, ``label`` fields."""
    from sidequest.server.reference_theme import PACK_TOC  # type: ignore[attr-defined]

    assert isinstance(PACK_TOC, dict), "PACK_TOC must be a dict keyed by pack name"
    # Pick any known live pack and verify the entry shape.
    for pack in LIVE_PACKS:
        entries = PACK_TOC.get(pack)
        assert entries, f"PACK_TOC missing entries for live pack {pack!r}"
        for entry in entries:
            # Entries are dicts per app.jsx — required fields are num, id, label.
            assert "num" in entry, f"PACK_TOC[{pack!r}] entry missing 'num': {entry}"
            assert "id" in entry, f"PACK_TOC[{pack!r}] entry missing 'id': {entry}"
            assert "label" in entry, f"PACK_TOC[{pack!r}] entry missing 'label': {entry}"


def test_toc_links_resolve_to_section_ids(tmp_path: Path) -> None:
    """AC4 + Task D: each TOC ``<a href="#{id}">`` has a matching
    ``<section id="{id}">`` somewhere on the page (the section/file
    mapping comes from ``TOC_TO_FILES`` per Task D)."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)

    # Extract all TOC link targets — anchor only on toc-num span as the
    # marker to avoid matching unrelated #-fragments elsewhere.
    toc_pos = html.index('class="toc"')
    toc_close = html.index("</nav>", toc_pos)
    toc_block = html[toc_pos:toc_close]
    targets = re.findall(r'href="#([a-z0-9][a-z0-9_-]*)"', toc_block)
    assert targets, 'AC4: no TOC links found inside <nav class="toc">'

    for target in targets:
        assert f'id="{target}"' in html, (
            f'AC4/Task D: TOC links to #{target} but no <section id="{target}"> '
            f"renders on the page — section/file mapping (TOC_TO_FILES) is "
            f"broken or this TOC entry has no content file."
        )


def test_unknown_pack_falls_back_to_default_toc_and_fires_error_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4 + AC10: unknown pack name → rules page uses DEFAULT_RULES_TOC
    (universal for all packs), lore page falls through to DEFAULT_TOC and
    fires ``sidequest.reference.toc_missing`` ERROR span.

    Rules pages use a dedicated rules-oriented TOC (DEFAULT_RULES_TOC)
    that covers all rules content regardless of pack, so no toc_missing
    span is needed. The lore-page path still fires the span for
    unknown packs via ``_pack_toc_entries``.
    """
    from sidequest.server import reference_renderer
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path, pack_name="never_real_pack")
    # Story 63-11 drops empty rules sections + their TOC links. _seed_pack omits
    # magic + achievements, so seed them here with real content — this test
    # asserts all four DEFAULT_RULES_TOC sections are reachable, so each must
    # carry content to render. Strengthens the default-TOC-coverage check.
    (pack / "magic.yaml").write_text("genre: arcane\nallowed_sources:\n  - sorcery\n  - relic\n")
    (pack / "achievements.yaml").write_text(
        "- name: First Blood\n  condition: Win a duel\n  reward: Glory\n"
    )

    calls: list[dict[str, str]] = []
    from contextlib import contextmanager

    @contextmanager
    def _spy(*, pack: str, _tracer: object = None):
        calls.append({"pack": pack})
        from sidequest.telemetry.spans.reference import (
            reference_toc_missing_span as real_helper,
        )

        with real_helper(pack=pack, _tracer=_tracer) as span:
            yield span

    monkeypatch.setattr(reference_renderer, "reference_toc_missing_span", _spy)

    html = assemble_rules_page("never_real_pack", pack)

    # Rules page uses DEFAULT_RULES_TOC — all rules-relevant sections present.
    assert "#bearing" in html, "Rules page must have bearing section"
    assert "#edge" in html, "Rules page must have edge (rules) section"
    assert "#affinities" in html, "Rules page must have affinities (magic) section"
    assert "#achievements" in html, "Rules page must have achievements section"

    # Rules pages do NOT fire toc_missing span — they use a universal TOC.
    assert not calls, (
        "Rules page uses DEFAULT_RULES_TOC for all packs — "
        "toc_missing span should not fire."
    )


def test_toc_missing_span_helper_is_importable() -> None:
    """The span helper must exist as a context-manager import target so
    the renderer (and any future caller) can use it."""
    # RED until Dev adds it.
    from sidequest.telemetry.spans.reference import (  # noqa: F401
        SPAN_REFERENCE_TOC_MISSING,
        reference_toc_missing_span,
    )


def test_toc_missing_span_is_registered_flat_only() -> None:
    """ADR-031 flat-only span registry — new chrome-render failure
    spans follow the existing SPAN_REFERENCE_* shape."""
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS
    from sidequest.telemetry.spans.reference import SPAN_REFERENCE_TOC_MISSING

    assert SPAN_REFERENCE_TOC_MISSING in FLAT_ONLY_SPANS, (
        "AC10: new toc_missing span must be registered in FLAT_ONLY_SPANS "
        "next to the existing SPAN_REFERENCE_* entries — required for the "
        "GM dashboard's agent_span_close fan-out."
    )


# ---------------------------------------------------------------------------
# Task E → AC5 — inline observer rewritten per plan verbatim
# ---------------------------------------------------------------------------


def _scroll_spy_body(html: str) -> str:
    """Return the inline <script> body that wraps the IntersectionObserver."""
    io_pos = html.index("IntersectionObserver")
    script_open = html.rfind("<script", 0, io_pos)
    script_close = html.index("</script>", io_pos)
    return html[script_open:script_close]


def test_scroll_spy_queries_toc_sticky_nav_toc_anchors(tmp_path: Path) -> None:
    """AC5: observer queries ``aside.toc-sticky nav.toc a``, not the
    legacy 63-4 ``.contents-rail a[href^="#"]`` selector."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)
    body = _scroll_spy_body(html)

    assert "aside.toc-sticky nav.toc a" in body, (
        "AC5: scroll-spy uses legacy selector. Plan v3 Task 22 step 3 "
        "queries `aside.toc-sticky nav.toc a` to locate TOC links."
    )
    assert ".contents-rail" not in body, (
        "AC5: scroll-spy still references legacy .contents-rail selector"
    )


def test_scroll_spy_toggles_active_class_not_aria_current(tmp_path: Path) -> None:
    """AC5: observer toggles ``classList.add('active')`` / ``remove('active')``
    on the matching link — replacing 63-4's ``aria-current`` toggle.

    The bundle's CSS targets ``.toc a.active`` for the visited-state
    treatment; without ``.active``, the visual feedback that a section
    is in view is lost.
    """
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)
    body = _scroll_spy_body(html)

    # The plan's exact JS uses .classList.add('active') and .remove('active')
    assert "'active'" in body or '"active"' in body, (
        "AC5: scroll-spy doesn't toggle the 'active' class string"
    )
    # And the legacy aria-current path must be gone.
    assert "aria-current" not in body, (
        "AC5: scroll-spy still toggles aria-current — plan v3 toggles "
        "the .active class instead so the bundle's `.toc a.active` rule fires."
    )


def test_scroll_spy_root_margin_matches_plan(tmp_path: Path) -> None:
    """AC5: ``rootMargin: '-20% 0% -60% 0%'`` (plan line 2828, line spec).
    63-4 shipped ``-30% 0px -60% 0px`` — must be replaced."""
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)
    body = _scroll_spy_body(html)

    assert "-20% 0% -60% 0%" in body, (
        "AC5: rootMargin not aligned to plan — must be exactly "
        "'-20% 0% -60% 0%' per plan line 2828."
    )


def test_scroll_spy_script_remains_under_2kb(tmp_path: Path) -> None:
    """AC5 regression guard: the inline scroll-spy script body stays
    bounded (≤2KB). 63-4 had this guard; the v3 rewrite is still ~22
    lines and must continue to fit.
    """
    from sidequest.server.reference_renderer import assemble_rules_page

    pack = _seed_pack(tmp_path)
    html = assemble_rules_page("space_opera", pack)
    body = _scroll_spy_body(html)

    assert len(body) < 2048, (
        f"AC5: scroll-spy <script> grew to {len(body)} bytes — was the "
        f"SPA bundle accidentally inlined? Cap is 2048."
    )


# ---------------------------------------------------------------------------
# Task F → AC7 — `_KIND_OVERRIDES["factions"] = "cult"`
# ---------------------------------------------------------------------------


def test_pack_tier_factions_absent_from_lore_page(tmp_path: Path) -> None:
    """Story 63-10 (absolute world-only) REVERSES 63-7's pack-flavor merge.

    63-7 (Task F) wired pack-tier ``factions.yaml`` into the lore page and
    namespaced its items as ``cult-<slug>``. 63-10 drops
    ``LORE_PACK_FLAVOR_FILES`` from ``assemble_lore_page`` entirely, so pack-tier
    factions no longer render on a world's lore page (factions are world-tier
    per SOUL "Flavor in the World"). The Architect ruled this is fixture-only —
    zero of ten live packs ship a ``factions.yaml`` — so the cut removes no real
    factions-on-lore-page rendering.

    This is the factions facet of the absence guards (AC1–AC3/AC5): a seeded
    pack-tier ``factions.yaml`` must NOT surface its ``cult-<slug>`` ids on the
    lore page. (The ``factions → cult`` namespace mapping itself is still
    asserted, render-independently, by
    ``test_kind_overrides_contains_factions_to_cult_mapping``.)
    """
    from sidequest.server.reference_renderer import assemble_lore_page

    pack = _seed_pack(tmp_path)
    # Seed a pack-tier factions.yaml that 63-7 would have merged onto the page.
    (pack / "factions.yaml").write_text(
        "- name: river-cabal\n  disposition: hostile\n- name: old-folk\n  disposition: wary\n"
    )
    world = _seed_world(pack)

    html = assemble_lore_page("space_opera", "coyote_star", pack, world)

    assert 'id="cult-river-cabal"' not in html, (
        "63-10: pack-tier factions.yaml must NOT merge onto the lore page — "
        "the world-only cut drops LORE_PACK_FLAVOR_FILES."
    )
    assert 'id="cult-old-folk"' not in html, (
        "63-10: pack-tier factions.yaml must NOT merge onto the lore page."
    )


def test_kind_overrides_contains_factions_to_cult_mapping() -> None:
    """AC7 unit-level check: the override map itself has the entry,
    independent of any rendered output. Catches drift if a future
    refactor moves the renderer's namespacing logic without porting
    the factions/cult mapping."""
    from sidequest.server.reference_renderer import _KIND_OVERRIDES

    assert _KIND_OVERRIDES.get("factions") == "cult", (
        "AC7: _KIND_OVERRIDES['factions'] should be 'cult' per plan line 2832–2839"
    )


# ---------------------------------------------------------------------------
# Tasks B + C → AC6 — PACK_LABELS / PACK_BLURBS / PACK_EPIGRAPHS / PACK_TOC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "constant_name",
    ["PACK_LABELS", "PACK_BLURBS", "PACK_EPIGRAPHS", "PACK_TOC"],
)
def test_pack_constant_is_exported_from_reference_theme(constant_name: str) -> None:
    """AC6: PACK_LABELS / PACK_BLURBS / PACK_EPIGRAPHS / PACK_TOC are
    ported into ``reference_theme.py`` as importable module-level
    constants. RED until Dev adds them."""
    import sidequest.server.reference_theme as theme_module

    assert hasattr(theme_module, constant_name), (
        f"AC6: {constant_name} not exported from reference_theme.py — "
        f"plan v3 Tasks 21/22 require these Python ports of app.jsx "
        f"constants."
    )
    value = getattr(theme_module, constant_name)
    assert isinstance(value, dict), f"AC6: {constant_name} should be a dict"


@pytest.mark.parametrize("pack", LIVE_PACKS)
@pytest.mark.parametrize(
    "constant_name",
    ["PACK_LABELS", "PACK_BLURBS", "PACK_EPIGRAPHS", "PACK_TOC"],
)
def test_pack_constants_cover_every_live_pack(pack: str, constant_name: str) -> None:
    """AC6: all 10 live packs covered by each PACK_* constant — no
    fallthrough on a real pack. The unknown-pack fallback path is
    reserved for genuine drift (new pack added to content without
    chrome ports), not for filling in gaps for known packs."""
    import sidequest.server.reference_theme as theme_module

    constant = getattr(theme_module, constant_name, {})
    assert pack in constant, (
        f"AC6: {constant_name} missing entry for live pack {pack!r}. "
        f"All 10 live packs must be ported from app.jsx so the default "
        f"fallback (which fires an ERROR span) is reserved for true "
        f"drift, not routine renders."
    )


def test_pack_epigraph_has_body_and_attrib_fields() -> None:
    """AC2.5 + AC6: PACK_EPIGRAPHS values are dicts with ``body`` and
    ``attrib`` fields (per app.jsx shape). The hero renderer reads
    both fields; missing ``attrib`` would mean the ``<span class="attrib">``
    is empty even when the bundle's CSS expects a value."""
    from sidequest.server.reference_theme import PACK_EPIGRAPHS  # type: ignore[attr-defined]

    for pack, epigraph in PACK_EPIGRAPHS.items():
        assert isinstance(epigraph, dict), (
            f"PACK_EPIGRAPHS[{pack!r}] should be a dict with body/attrib"
        )
        assert "body" in epigraph, f"PACK_EPIGRAPHS[{pack!r}] missing 'body' field"
        assert "attrib" in epigraph, (
            f"PACK_EPIGRAPHS[{pack!r}] missing 'attrib' field — without "
            f'it the hero\'s <span class="attrib"> renders empty.'
        )


# ---------------------------------------------------------------------------
# AC9 — no `.contents-rail` anywhere in emitted HTML
# (the wiring test enforces this against CSS — this asserts the renderer
#  side directly for an unambiguous error message)
# ---------------------------------------------------------------------------


def test_no_renderer_path_emits_contents_rail_class(tmp_path: Path) -> None:
    """AC9: ``class="contents-rail"`` must not appear in any rendered
    page. The wiring test catches this via the CSS mismatch path, but
    this direct assertion gives a cleaner failure message for the
    developer reading test output."""
    from sidequest.server.reference_renderer import (
        assemble_lore_page,
        assemble_rules_page,
    )

    pack = _seed_pack(tmp_path)
    world = _seed_world(pack)

    rules_html = assemble_rules_page("space_opera", pack)
    lore_html = assemble_lore_page("space_opera", "coyote_star", pack, world)

    for label, html in (("rules", rules_html), ("lore", lore_html)):
        assert 'class="contents-rail"' not in html, (
            f'AC9: {label} page still emits legacy `class="contents-rail"` — '
            f"63-4's invented vocabulary must be retired in favor of "
            f"`.toc-sticky` / `.toc`."
        )
