"""Story 63-7 Task G — KEYSTONE wiring test for reference-page chrome.

Why this file exists
--------------------
Story 63-4 shipped reference-page chrome whose emitted markup vocabulary
(``class="contents-rail"``, bare ``<h1>``, ``<p class="epigraph">``…) does
**not** match the class names the bundled CSS targets
(``.toc-sticky``, ``.toc``, ``.hero-title``, ``.hero-eyebrow``,
``.hero-epigraph``, ``.attrib`` …). Result: production pages at
``sidequest.slabgorb.com/reference/...`` render with browser defaults
instead of the design bundle's parchment/terminal typography.

63-4's chrome tests went green because they asserted only that the
strings ``class="contents-rail"`` and ``class="hero"`` appeared somewhere
in the HTML. They never compared the rendered class names against the
served CSS bundle — so the drift slipped through.

This file is the regression guard 63-4 lacked: it parses every
``class="..."`` token the renderer emits across the lore and rules
pages, and asserts that each class either appears as a selector in the
served CSS bundle OR is in the deliberately-small ``SEMANTIC_ALLOWLIST``
(state hooks the JS toggles at runtime, plus the ``dark`` class on
``<html>`` that the bundle's ``[data-archetype=…]`` rules read).

Failure mode
------------
If this test ever fails again, the renderer is emitting decorative
classes that won't render — the exact same failure mode that produced
story 63-7. Either:

1. Restore the bundle's vocabulary on the renderer side (the usual fix).
2. Add a CSS rule to the bundle for the new class (rare; the bundle is
   the source of truth, not the renderer).
3. Add the class to ``SEMANTIC_ALLOWLIST`` with a comment explaining
   why it has no styling. Keep the allowlist short — every entry is a
   tiny window for future drift.

Scope notes
-----------
- This is a SUBSTRING check against the concatenated CSS text, not a
  proper CSS parser. False positives (class name appears inside a
  comment but not as a selector) are extremely rare for the bundle's
  shape; false negatives (a real bug slips through) are what we
  actually care about, and substring matching catches the load-bearing
  case (``.contents-rail`` → no occurrence anywhere in the CSS text).
- The test reads the **served** CSS files at
  ``sidequest/server/static/reference/{theme,styles}.css`` (committed
  in the server tree). It does NOT read content-side YAML. This is
  intentional per the no-content-coupled-tests memo — the CSS bundle
  is product, not content.
- The test renders against a tmp fixture pack named ``space_opera``
  (a known PACK_TOC key) so the TOC populates the same vocabulary the
  bundle styles. Other pack names would exercise the unknown-pack
  fallback path, which is covered separately in
  ``test_reference_chrome_v3.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

# Allowlist for classes the renderer emits that intentionally have no
# matching CSS rule. EVERY entry needs a one-line justification.
#
# Keep this list SHORT (target ≤5). Each addition is a deliberate
# acknowledgement that future drift could re-emit decorative-but-
# unstyled markup; the wiring test will not catch it for these names.
SEMANTIC_ALLOWLIST: set[str] = {
    # Toggle class the inline scroll-spy script adds at runtime to the
    # currently-visible TOC link. The bundle styles `.toc a.active`,
    # which substring-matches `.active` already, so this entry is
    # belt-and-braces (the substring check passes today). Keep it
    # explicit so a future CSS refactor that strips `.toc a.active`
    # doesn't quietly let the renderer keep emitting `.active`.
    "active",
    # `class="dark"` on the `<html>` element — the bundle's archetype
    # rules read `[data-archetype="..."]` but the dark-mode opt-in is
    # an inherited convention from the design bundle's `app.jsx`.
    # No `.dark` selector in the CSS; semantic-only.
    "dark",
    # `<section class="file" id="file-{stem}">` is a structural marker
    # the renderer emits around each rendered YAML file. The bundle has
    # no visual treatment for `.file` (file boundaries should be
    # invisible in the rendered chrome), but the class is load-bearing
    # for the test suite and any future content tooling that needs to
    # walk the per-file boundaries (cross-anchor checks, validators,
    # etc.). Removing the class would force every test that locates a
    # file section to read the `id="file-…"` attribute instead — more
    # brittle than a stable class marker. Semantic-only by design.
    "file",
}


_FIXTURE_THEME_YAML = (
    # space_opera palette is a placeholder — the renderer only reads
    # these as ``--ref-*`` CSS variables and doesn't validate values.
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


def _seed_space_opera_pack(tmp_path: Path) -> Path:
    """Create a tmp pack dir named ``space_opera`` with enough content
    to exercise both rules and lore page renders. Pack name is chosen
    to match a known PACK_TOC key once Task C lands; before then, the
    rendered pages still produce class tokens for the wiring test to
    inspect (the unknown-pack default-TOC path is its own test)."""
    pack = tmp_path / "space_opera"
    pack.mkdir(parents=True)
    (pack / "theme.yaml").write_text(_FIXTURE_THEME_YAML)
    (pack / "archetypes.yaml").write_text("kinds:\n  - spacer\n  - colonist\n")
    (pack / "classes.yaml").write_text(
        "- name: pilot\n  signature: vector-burn\n- name: scavver\n  signature: ledger-bargain\n"
    )
    (pack / "cultures.yaml").write_text("- name: vacworld-born\n  language: jovian-pidgin\n")
    (pack / "factions.yaml").write_text("- name: old-folk\n  disposition: wary\n")
    (pack / "rules.yaml").write_text("core: vector-and-trust\n")
    return pack


def _seed_space_opera_world(pack_dir: Path) -> Path:
    """Seed a lore-bearing world dir so ``_build_hero`` reaches the
    full happy path (world_name + epigraph present)."""
    world = pack_dir / "worlds" / "coyote_star"
    world.mkdir(parents=True)
    (world / "world.yaml").write_text("name: Coyote Star\n")
    (world / "lore.yaml").write_text(
        "world_name: Coyote Star\n"
        "epigraph: Out here the only law that travels faster than light is grief.\n"
    )
    (world / "legends.yaml").write_text("- name: the-long-burn\n  origin: pre-collapse\n")
    (world / "locations.yaml").write_text("- name: the-broken-needle\n  district: belt\n")
    return world


def _extract_emitted_classes(html: str) -> set[str]:
    """Return every distinct class token the renderer emitted in ``html``.

    Walks every ``class="..."`` attribute, splits each value on whitespace,
    and unions the tokens. Empty strings and pure whitespace are dropped.
    """
    tokens: set[str] = set()
    for match in re.finditer(r'class="([^"]+)"', html):
        for token in match.group(1).split():
            stripped = token.strip()
            if stripped:
                tokens.add(stripped)
    return tokens


def _served_css_text() -> str:
    """Read the bundled CSS files the renderer links to.

    The reference renderer emits ``<link>`` tags for ``theme.css``,
    ``styles.css``, and ``presenters.css`` — see ``_wrap_document`` in
    ``reference_renderer.py``. The corresponding on-disk files are below
    ``sidequest/server/static/reference/``. If any file is missing, the
    bundle has been broken upstream and the test should fail loud.
    """
    # tests/server/ → tests/ → sidequest-server/
    repo_root = Path(__file__).resolve().parents[2]
    base = repo_root / "sidequest" / "server" / "static" / "reference"
    theme = base / "theme.css"
    styles = base / "styles.css"
    presenters = base / "presenters.css"
    if not theme.is_file():
        raise FileNotFoundError(
            f"Served CSS missing: {theme}. The wiring test cannot compare "
            f"emitted classes against an absent CSS bundle."
        )
    if not styles.is_file():
        raise FileNotFoundError(f"Served CSS missing: {styles}.")
    if not presenters.is_file():
        raise FileNotFoundError(f"Served CSS missing: {presenters}.")
    return (
        theme.read_text(encoding="utf-8")
        + "\n"
        + styles.read_text(encoding="utf-8")
        + "\n"
        + presenters.read_text(encoding="utf-8")
    )


def test_every_emitted_class_has_matching_css_rule(tmp_path: Path) -> None:
    """KEYSTONE: every class the renderer emits is styled by the bundle.

    Renders the fixture lore + rules pages, collects every distinct
    ``class="..."`` token they emit, then verifies each token either:

    1. appears as a substring ``.{token}`` somewhere in the
       concatenated ``theme.css`` + ``styles.css`` text, OR
    2. is in ``SEMANTIC_ALLOWLIST`` (state hooks the JS toggles, etc.)

    The substring check is intentionally cheap. A CSS parser dep would
    be overkill for a regression guard whose load-bearing failure mode
    is "we shipped `.contents-rail` again."

    Why this fails RED today: ``_build_contents_rail`` emits
    ``class="contents-rail"``, but ``grep contents-rail
    sidequest/server/static/reference/styles.css`` returns zero matches.
    The wiring test catches that exact drift.
    """
    from sidequest.server.reference_renderer import (
        assemble_lore_page,
        assemble_rules_page,
    )

    pack = _seed_space_opera_pack(tmp_path)
    world = _seed_space_opera_world(pack)

    rules_html = assemble_rules_page("space_opera", pack)
    lore_html = assemble_lore_page("space_opera", "coyote_star", pack, world)

    emitted = _extract_emitted_classes(rules_html) | _extract_emitted_classes(lore_html)
    assert emitted, "No class attributes found in rendered HTML — fixture broken?"

    css_text = _served_css_text()

    unmatched = sorted(
        cls for cls in emitted if cls not in SEMANTIC_ALLOWLIST and f".{cls}" not in css_text
    )

    assert not unmatched, (
        f"Renderer emits {len(unmatched)} class name(s) with no matching "
        f"selector in the served CSS bundle (theme.css + styles.css):\n"
        f"  {unmatched}\n\n"
        f"This is the same failure mode story 63-7 was created to fix: the "
        f"renderer ships markup the bundle's CSS cannot style, so production "
        f"pages render with browser defaults.\n\n"
        f"To resolve, either:\n"
        f"  1. Conform the renderer to the bundle's vocabulary (the usual "
        f"fix — see plan 2026-05-23-reference-pages-v3.md Tasks 20–22).\n"
        f"  2. Add a CSS rule to the bundle (rare — bundle is source of "
        f"truth, not the renderer).\n"
        f"  3. Add the class to SEMANTIC_ALLOWLIST in this test file with "
        f"a one-line justification.\n"
    )


def test_renderer_does_not_emit_legacy_contents_rail_class(tmp_path: Path) -> None:
    """Belt-and-braces: ensure the specific 63-4 drift class is gone.

    The wiring test above would catch ``contents-rail`` as an unmatched
    class. This explicit sentinel makes the failure message unambiguous
    for the developer reading test output: if THIS test fails, the
    legacy 63-4 vocabulary is still being emitted. Once green, this
    test stays as a guard against accidental revival.
    """
    from sidequest.server.reference_renderer import (
        assemble_lore_page,
        assemble_rules_page,
    )

    pack = _seed_space_opera_pack(tmp_path)
    world = _seed_space_opera_world(pack)

    rules_html = assemble_rules_page("space_opera", pack)
    lore_html = assemble_lore_page("space_opera", "coyote_star", pack, world)

    for label, html in (("rules", rules_html), ("lore", lore_html)):
        assert 'class="contents-rail"' not in html, (
            f'{label} page still emits the legacy 63-4 `class="contents-rail"`. '
            f"Plan v3 Task 22 replaces it with "
            f'`<aside class="toc-sticky"><nav class="toc">…</nav></aside>` '
            f'wrapped inside `<div class="layout">`.'
        )


def test_semantic_allowlist_stays_small() -> None:
    """Guard against allowlist growth. Every entry is a window for drift.

    The allowlist exists for genuine runtime-only / inheritance-only
    state hooks. If it grows past a small cap, the wiring test is no
    longer doing its job — entries are accumulating instead of being
    addressed at the renderer or CSS layer.
    """
    assert len(SEMANTIC_ALLOWLIST) <= 5, (
        f"SEMANTIC_ALLOWLIST has {len(SEMANTIC_ALLOWLIST)} entries "
        f"({sorted(SEMANTIC_ALLOWLIST)}) — cap is 5. Either the renderer "
        f"is emitting too many unstyled classes (fix the renderer) or "
        f"the CSS bundle is missing rules the renderer expects (fix the "
        f"bundle). Do not raise this cap silently."
    )
