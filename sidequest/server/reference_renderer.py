"""Render parsed-YAML trees as a hypertext document.

Pure functions only. The HTTP boundary (404 / 500 / file IO) lives in
reference_routes.py. This module just walks dict/list/scalar trees and produces
HTML fragments.

Headings get stable slugified ``id`` attributes so future deep-link work can
target them without schema changes.

**Chrome contract (Story 63-7).** The bundled CSS at
``sidequest/server/static/reference/{theme,styles}.css`` is the source of
truth for the markup vocabulary. This renderer conforms to it — never the
other way around. Specifically:

- ``<body>`` content (everything except the trailing scroll-spy script)
  sits inside ``<div class="page">…</div>`` (Task A).
- Hero is ``<header class="hero">`` containing five ordered elements:
  ``.hero-eyebrow`` (glyph + eyebrow.gilt + rule), ``.hero-kicker``,
  ``<h1 class="hero-title">``, ``.hero-sub``, and
  ``.hero-epigraph narrative-flourish`` with a ``<span class="attrib">``
  (Task B). Both lore and rules pages emit a hero — rules-page hero
  title falls back to ``PACK_LABELS[pack]``.
- Body is wrapped in ``<div class="layout"><aside class="toc-sticky">
  <nav class="toc"><ol>…</ol></nav></aside><main>…</main></div>`` and
  each TOC entry has a matching ``<section id="{toc.id}">…</section>``
  inside ``<main>`` (Task C, D).
- Inline scroll-spy queries ``aside.toc-sticky nav.toc a``, toggles
  ``.active`` on the visible link, ``rootMargin '-20% 0% -60% 0%'``
  (Task E).
- ``_KIND_OVERRIDES["factions"] = "cult"`` for lore-tier list-of-dict
  namespacing (Task F).

The ``test_reference_chrome_wiring.py`` regression guard parses every
emitted ``class="…"`` and asserts each matches a CSS rule — drift like
shipping ``.contents-rail`` again will fail loud.
"""

from __future__ import annotations

import json
import logging
import re
from html import escape
from pathlib import Path

import yaml

from sidequest.server.reference_presenters import (
    PresenterContext,
    lookup_presenter,
)
from sidequest.server.reference_slug import slugify
from sidequest.server.reference_theme import (
    DEFAULT_RULES_TOC,
    DEFAULT_TOC,
    PACK_BLURBS,
    PACK_EPIGRAPHS,
    PACK_LABELS,
    PACK_TOC,
    TOC_TO_FILES,
    ReferenceTheme,
    load_reference_theme,
)
from sidequest.server.reference_visibility import Visibility, classify
from sidequest.telemetry.spans.reference import (
    reference_hero_unbound_span,
    reference_presenter_error_span,
    reference_toc_missing_span,
    reference_unknown_field_span,
    reference_unpresented_field_span,
)

_DEPTH_CAP = 6

_logger = logging.getLogger(__name__)
_unknown_logged: set[tuple[str, tuple[str, ...]]] = set()


def _log_unknown_once(file_stem: str, key_path: tuple[str, ...]) -> None:
    """Stderr-warn once per (stem, key_path) per process so logs don't spam.
    OTEL span still fires every render."""
    key = (file_stem, key_path)
    if key in _unknown_logged:
        return
    _unknown_logged.add(key)
    _logger.warning(
        "reference: unclassified field (%s, %s) — dropping. "
        "Add to reference_visibility.PUBLIC or KEEPER.",
        file_stem,
        ".".join(key_path) or "<root>",
    )


# Singularised stems for files whose entries are individually named items.
# Entries inside these files become `<kind>-<slug>` anchors so cross-file
# name collisions cannot produce duplicate ids.
_KIND_OVERRIDES: dict[str, str] = {
    "classes": "class",
    "archetypes": "archetype",
    "cultures": "culture",
    "legends": "legend",
    "locations": "location",
    "achievements": "achievement",
    # Story 63-7 Task F: factions.yaml entries get `cult-<slug>` ids
    # (per plan line 2832-2839). Keeps lore-tier list-of-dict items
    # in a distinct namespace from rules-tier classes/archetypes.
    "factions": "cult",
}


def _kind_for_stem(stem: str) -> str:
    """Return the namespaced anchor kind for a given file stem.

    Default behaviour is to use the stem as-is (e.g. `history` → `history`),
    so non-plural files still benefit from file-level namespacing while
    pluralised files get a cleaner singular form.
    """
    return _KIND_OVERRIDES.get(stem, stem)


def render_node(
    node: object,
    depth: int = 0,
    kind: str | None = None,
    *,
    ctx: PresenterContext | None = None,
) -> str:
    """Render a parsed-YAML node to an HTML fragment.

    Handles: dict (recursive nested <section>), list (ul for scalars, sectioned
    for dicts), scalar (str/int/float/bool/None). When ``depth`` reaches
    ``_DEPTH_CAP`` for a non-empty container, the subtree is dumped as YAML
    inside a ``<pre>`` block instead of recursing into runaway markup.

    ``kind`` is threaded from ``_render_file`` to namespace list-of-dict anchor
    ids. Top-level dict keys and recursively nested keys always use flat slugs;
    only the direct children of a top-level list are namespaced.

    ``ctx`` carries the presenter context (pack, world, file_stem, key_path,
    theme, depth) for visibility classification and presenter dispatch. When
    None (e.g. synthetic callers), classification is skipped and all fields
    render via the generic fallback.
    """
    if depth >= _DEPTH_CAP and isinstance(node, (dict, list)) and node:
        dumped = yaml.safe_dump(node, sort_keys=False, default_flow_style=False)
        return f"<pre>{escape(dumped)}</pre>"
    if isinstance(node, dict):
        return _render_dict(node, depth, kind=kind, ctx=ctx) if node else "<p><em>(empty)</em></p>"
    if isinstance(node, list):
        return _render_list(node, depth, kind=kind, ctx=ctx) if node else "<p><em>(empty)</em></p>"
    return _render_scalar(node)


def _humanize_label(raw: object) -> str:
    """Humanize an identifier-shaped key/id for a player-facing heading.

    The generic fallback printed raw YAML keys and item ids verbatim as
    headings — ``the_maw``, ``genre_conventions``, ``rolls_per_slot``,
    ``floor_it`` — leaking snake_case onto Rules/Lore pages (playtest
    2026-05-27, the dominant road_warrior failure). This converts
    identifier-shaped strings to Title Case ("The Maw", "Genre Conventions",
    "Rolls Per Slot", "Floor It").

    Conservative by design: a string that already contains whitespace OR any
    uppercase letter is assumed author-formatted and returned UNCHANGED, so a
    real heading like "Floor It", an acronym, or a proper noun is never
    mangled (no "USB" → "Usb"). Only pure identifier tokens are transformed.
    """
    text = str(raw).strip()
    if not text:
        return text
    if any(c.isspace() for c in text) or any(c.isupper() for c in text):
        return text
    parts = [p for p in re.split(r"[_\-]+", text) if p]
    return " ".join(p.capitalize() for p in parts) or text


def _render_scalar(value: object) -> str:
    if value is None:
        return "<p><em>(none)</em></p>"
    text = str(value)
    if "\n" in text:
        return f'<p class="multiline">{escape(text)}</p>'
    return f"<p>{escape(text)}</p>"


# Note: nested dicts recurse into nested <section> elements via render_node, not
# <dl>/<dt>/<dd>. This is intentional per the plan; the spec design doc's
# <dl> note is out of date and will be reconciled to match.
def _render_dict(
    node: dict,
    depth: int,
    kind: str | None = None,
    *,
    ctx: PresenterContext | None = None,
) -> str:
    parts: list[str] = []
    for key, value in node.items():
        slug = slugify(str(key))
        child_path = (ctx.key_path if ctx else ()) + (str(key),)
        child_ctx = (
            PresenterContext(
                pack=ctx.pack,
                world=ctx.world,
                file_stem=ctx.file_stem,
                key_path=child_path,
                theme=ctx.theme,
                depth=depth + 1,
                poi_image_slugs=ctx.poi_image_slugs,
            )
            if ctx
            else None
        )

        # Visibility gate (only when we have a context — real file walk).
        if ctx is not None:
            vis = classify(ctx.file_stem, child_path)
            if vis is Visibility.KEEPER:
                # Silent drop — intentional. Load-time validation covers this.
                continue
            if vis is Visibility.UNKNOWN:
                with reference_unknown_field_span(
                    pack=ctx.pack,
                    world=ctx.world,
                    file_stem=ctx.file_stem,
                    key_path=child_path,
                ):
                    pass
                _log_unknown_once(ctx.file_stem, child_path)
                continue

        # Presenter dispatch (only when we have ctx).
        presenter = (
            lookup_presenter(child_ctx.file_stem, child_ctx.key_path)
            if child_ctx is not None
            else None
        )
        if presenter is not None and child_ctx is not None:
            try:
                rendered_value = presenter(value, child_ctx)
            except Exception as exc:
                with reference_presenter_error_span(
                    pack=child_ctx.pack,
                    file_stem=child_ctx.file_stem,
                    key_path=child_ctx.key_path,
                ) as span:
                    span.record_exception(exc)
                raise
            if rendered_value:
                parts.append(rendered_value)
            continue

        # Generic fallback — fire INFO span for ctx-bearing renders.
        if ctx is not None:
            with reference_unpresented_field_span(
                pack=ctx.pack,
                file_stem=ctx.file_stem,
                key_path=child_path,
            ):
                pass
        # Dict keys always use flat slugs — only list-of-dict items are namespaced.
        parts.append(f'<section id="{slug}">')
        parts.append(f"<h2>{escape(_humanize_label(key))}</h2>")
        # Forward kind so it reaches lists nested inside this dict's values.
        parts.append(render_node(value, depth + 1, kind=kind, ctx=child_ctx))
        parts.append("</section>")
    return "".join(parts)


_NAME_FIELDS = ("name", "id", "title")


def _heading_for_item(item: dict, index: int, kind: str | None = None) -> tuple[str, str]:
    """Return (slug, display) for a list-of-dict item heading.

    Iterates name -> id -> title looking for a usable value. A value is
    "usable" when, after str() and strip(), it is non-empty. Empty strings
    fall through to the next field, then to "Item N". If the chosen value
    slugifies to "" (e.g. unicode-only), the index-based slug is used but
    the readable display is preserved.

    When ``kind`` is provided (threaded from ``_render_file``), the resulting
    slug is prefixed as ``<kind>-<slug>`` so same-named items in different files
    produce distinct anchor ids.
    """
    fallback_display = f"Item {index + 1}"
    fallback_slug = slugify(fallback_display)
    if kind:
        fallback_slug = f"{kind}-{fallback_slug}"
    for field in _NAME_FIELDS:
        raw = item.get(field)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        slug = slugify(value)
        if not slug:
            # Display value survived but produced an empty slug (e.g. unicode-only).
            # Keep the readable display, fall back to the index-based slug so we
            # never emit id="".
            return fallback_slug, value
        if kind:
            slug = f"{kind}-{slug}"
        return slug, value
    return fallback_slug, fallback_display


# TODO(reference v2): two list items with the same name produce duplicate id
# attributes; acceptable for v1, fix with per-list seen-set when authoring
# friction surfaces it.
def _render_list(
    items: list,
    depth: int,
    kind: str | None = None,
    *,
    ctx: PresenterContext | None = None,
) -> str:
    if all(not isinstance(item, (dict, list)) for item in items):
        lis = "".join(f"<li>{escape(str(item))}</li>" for item in items)
        return f"<ul>{lis}</ul>"
    parts: list[str] = []
    for index, item in enumerate(items):
        if isinstance(item, dict):
            slug, display = _heading_for_item(item, index, kind=kind)
            child_ctx = (
                PresenterContext(
                    pack=ctx.pack,
                    world=ctx.world,
                    file_stem=ctx.file_stem,
                    key_path=ctx.key_path + ("*",),
                    theme=ctx.theme,
                    depth=depth + 1,
                    poi_image_slugs=ctx.poi_image_slugs,
                )
                if ctx
                else None
            )
            parts.append(f'<section id="{slug}">')
            parts.append(f"<h3>{escape(_humanize_label(display))}</h3>")
            # Nested dict keys are flat-slugged per the original design —
            # kind only namespaces direct list-of-dict items; do not forward.
            parts.append(render_node(item, depth + 1, ctx=child_ctx))
            parts.append("</section>")
        else:
            # Nested list — preserve existing behavior; no ctx forwarded
            # because the original recursed with no kind either.
            parts.append(render_node(item, depth + 1))
    return "".join(parts)


# --- File mapping ---
# File-to-page mapping (see spec: 2026-05-23-reference-pages-design.md §File-to-Page Mapping)
RULES_FILES: tuple[str, ...] = (
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

LORE_WORLD_FILES: tuple[str, ...] = (
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

LORE_PACK_FLAVOR_FILES: tuple[str, ...] = (
    "cultures.yaml",
    "lore.yaml",
    "history.yaml",
    # Story 63-7: factions live at the pack tier and namespace as `cult-*`.
    "factions.yaml",
)

EXCLUDED_FILES: frozenset[str] = frozenset(
    {
        # Spoiler-bearing / keeper-side only
        "npcs.yaml",
        "seed_tropes.yaml",
        "tropes.yaml",
        # System-tier / metadata / asset config (not player-facing content)
        "prompts.yaml",
        "pack.yaml",
        "theme.yaml",
        "visual_style.yaml",
        "audio.yaml",
        "portrait_manifest.yaml",
        "cartography.yaml",
        "axes.yaml",
        "lethality_policy.yaml",
        "visibility_baseline.yaml",
        "char_creation.yaml",
    }
)


# --- Page assemblers ---

# Matches only the lowercase-alnum-hyphen ids the renderer emits.  Single-quoted
# string literals inside the inline script (e.g. 'ref-anchors') are NOT preceded
# by `id=` so they cannot false-match.
_ID_ATTR_RE = re.compile(r'\bid="([a-z0-9][a-z0-9_-]*)"')


# Inline IntersectionObserver scroll-spy (Story 63-7 Task E — verbatim port
# from plan lines 2807-2828). Queries ``aside.toc-sticky nav.toc a`` to find
# TOC links and toggles ``classList.add('active')`` / ``.remove('active')``
# on the link whose target ``<section id>`` is highest in the viewport.
#
# ``rootMargin '-20% 0% -60% 0%'`` defines a 20%-to-40% activation band:
# a section becomes "active" as it crosses the 20% line and stays active
# until it falls past the 40% line. This is the bundle's exact value.
#
# Bounded ≤2KB so a future inlined SPA bundle would trip the guard test.
_SCROLL_SPY_SCRIPT = (
    "<script>"
    "(function(){"
    "var ids=Array.from("
    "document.querySelectorAll('aside.toc-sticky nav.toc a')"
    ").map(function(a){return a.getAttribute('href').slice(1);});"
    "var sections=ids.map(function(id){return document.getElementById(id);})"
    ".filter(Boolean);"
    "var links={};"
    "document.querySelectorAll('aside.toc-sticky nav.toc a').forEach("
    "function(a){links[a.getAttribute('href').slice(1)]=a;}"
    ");"
    "if(!sections.length)return;"
    "var obs=new IntersectionObserver(function(entries){"
    "var visible=entries.filter(function(e){return e.isIntersecting;})"
    ".sort(function(a,b){return a.boundingClientRect.top-b.boundingClientRect.top;});"
    "if(visible[0]){"
    "Object.values(links).forEach(function(a){a.classList.remove('active');});"
    "var top=visible[0].target.id;"
    "if(links[top])links[top].classList.add('active');"
    "}"
    "},{rootMargin:'-20% 0% -60% 0%',threshold:0});"
    "sections.forEach(function(s){obs.observe(s);});"
    "})();"
    "</script>"
)


def _collect_anchor_ids(body: str) -> list[str]:
    """Return the deduplicated, source-order list of id values in ``body``."""
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _ID_ATTR_RE.finditer(body):
        anchor = match.group(1)
        if anchor in seen:
            continue
        seen.add(anchor)
        ordered.append(anchor)
    return ordered


_BAD_ANCHOR_BANNER = '<div id="ref-bad-anchor" hidden>Anchor not found on this page.</div>'

_BAD_ANCHOR_SCRIPT = (
    "<script>"
    "(function(){"
    "var h=location.hash.replace(/^#/,'');"
    "if(!h)return;"
    "var el=document.getElementById('ref-anchors');"
    "if(!el)return;"
    "var anchors=JSON.parse(el.textContent);"
    "if(anchors.indexOf(h)!==-1)return;"
    "var b=document.getElementById('ref-bad-anchor');"
    'b.textContent="Anchor \'#"+h+"\' not found on this page.";'
    "b.hidden=false;"
    "})();"
    "</script>"
)


def _stem_has_any_presenter(stem: str) -> bool:
    """True iff the PRESENTERS registry has at least one (stem, *) entry."""
    from sidequest.server.reference_presenters import PRESENTERS

    return any(reg_stem == stem for reg_stem, _ in PRESENTERS)


def _file_section_wrapper(path: Path, body: str) -> str:
    """Wrap file content in a ``<section class="file" id="file-{slug}">`` element.

    When the file stem has a registered presenter, the ``<h1>{filename}</h1>``
    file-header is suppressed — the TOC label provides the section title.
    Unpresented files keep the legacy ``<h1>`` so authors see the raw-fallback
    signal during development.

    The ``<section>`` anchor wrapper is always emitted so deep-links resolve.
    """
    file_slug = slugify(path.stem)
    if _stem_has_any_presenter(path.stem):
        return f'<section class="file" id="file-{file_slug}">{body}</section>'
    return (
        f'<section class="file" id="file-{file_slug}"><h1>{escape(path.name)}</h1>{body}</section>'
    )


def _render_file(
    path: Path,
    *,
    pack: str,
    world: str | None,
    theme: ReferenceTheme,
    poi_image_slugs: frozenset[str] = frozenset(),
) -> str:
    if not path.exists():
        return ""
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name}: malformed YAML: {exc}") from exc
    kind = _kind_for_stem(path.stem)
    ctx = PresenterContext(
        pack=pack,
        world=world,
        file_stem=path.stem,
        key_path=(),
        theme=theme,
        depth=0,
        poi_image_slugs=poi_image_slugs,
    )
    # File-root presenter dispatch (key_path == ()). Applies the same
    # visibility gate as _render_dict's child-key path. If KEEPER, drop
    # silently. If UNKNOWN, fire warn span + drop. Otherwise, run the
    # registered presenter (if any); absence means fall through to the
    # generic walk below.
    if data is not None:
        vis_root = classify(ctx.file_stem, ())
        if vis_root is Visibility.KEEPER:
            return ""
        if vis_root is Visibility.UNKNOWN:
            with reference_unknown_field_span(
                pack=ctx.pack,
                world=ctx.world,
                file_stem=ctx.file_stem,
                key_path=(),
            ):
                pass
            _log_unknown_once(ctx.file_stem, ())
            return ""
        presenter = lookup_presenter(path.stem, ())
        if presenter is not None:
            try:
                rendered = presenter(data, ctx)
            except Exception as exc:
                with reference_presenter_error_span(
                    pack=ctx.pack,
                    file_stem=ctx.file_stem,
                    key_path=(),
                ) as span:
                    span.record_exception(exc)
                raise
            if rendered:
                return _file_section_wrapper(path, rendered)
            # Presenter returned empty — means "I don't recognise this shape",
            # not "intentionally suppress". Fall through to the generic walk
            # below so the file content is never silently blackholed.

    body = "<p><em>(empty file)</em></p>" if data is None else render_node(data, kind=kind, ctx=ctx)
    return _file_section_wrapper(path, body)


def _render_file_with_label(
    path: Path,
    label: str,
    *,
    pack: str,
    world: str | None,
    theme: ReferenceTheme,
) -> str:
    """Like _render_file but appends a parenthetical label to the file heading.

    For presented files, the ``(genre)`` heading suffix is suppressed — the
    presenter output stands on its own and the TOC label is sufficient context.
    """
    rendered = _render_file(path, pack=pack, world=world, theme=theme)
    if not rendered:
        return ""
    if _stem_has_any_presenter(path.stem):
        # Presented file — TOC label suffices; suppress the legacy (genre)
        # heading suffix as well.
        return rendered
    return rendered.replace(
        f"<h1>{escape(path.name)}</h1>",
        f"<h1>{escape(path.name)} <small>{escape(label)}</small></h1>",
        1,
    )


def _theme_style_block(theme: ReferenceTheme) -> str:
    """Inline ``<style>`` block exposing per-pack palette + fonts as CSS vars
    so the bundled theme.css and styles.css can resolve them without hardcoded
    hex values. Per-pack values OVERRIDE any defaults in theme.css."""
    return (
        "<style>"
        ":root{"
        f"--ref-color-primary:{theme.palette_primary};"
        f"--ref-color-accent:{theme.palette_accent};"
        f"--ref-color-background:{theme.palette_background};"
        f"--ref-font-web:{theme.web_font_family};"
        f"--ref-font-display:{theme.display_font_family};"
        "}"
        "</style>"
    )


def _document_root_open(pack: str, world: str | None, archetype: str) -> str:
    """Open the ``<html>`` element with data-pack/world/archetype attrs that
    drive the bundle's per-archetype CSS rules ([data-archetype="rugged"], etc.)."""
    world_attr = f' data-world="{escape(world)}"' if world else ""
    return (
        '<html lang="en"'
        f' data-pack="{escape(pack)}"'
        f"{world_attr}"
        f' data-archetype="{escape(archetype)}"'
        ' class="dark">'
    )


# --- Chrome assemblers (Story 63-7) ---


def _pack_toc_entries(pack: str) -> list[dict[str, str]]:
    """Return the ordered TOC entries for ``pack``.

    Falls through to ``DEFAULT_TOC`` AND fires the
    ``sidequest.reference.toc_missing`` ERROR span when ``pack`` is absent
    from ``PACK_TOC``. Per plan line 2780 and AC10 — never silent.
    """
    entries = PACK_TOC.get(pack)
    if entries is None:
        with reference_toc_missing_span(pack=pack):
            return list(DEFAULT_TOC)
    return entries


def _build_toc(pack: str, *, toc_entries: list[dict[str, str]] | None = None) -> str:
    """Per-pack table of contents — emits
    ``<aside class="toc-sticky"><nav class="toc"><div class="toc-title">Contents</div><ol>…</ol></nav></aside>``.

    Each ``<li>`` is ``<a href="#{id}"><span class="toc-num">{num}.</span>{label}</a>``.
    The ``.toc-num`` class is what the bundle styles for the serif-numeral
    prefix; the ``<ol>`` (not ``<ul>``) is required because the bundle's
    ``.toc ol`` rule strips default list markers and ``.toc-num`` provides
    the visible numbering.
    """
    entries = toc_entries if toc_entries is not None else _pack_toc_entries(pack)
    items = "".join(
        f'<li><a href="#{escape(entry["id"])}">'
        f'<span class="toc-num">{escape(entry["num"])}.</span>'
        f"{escape(entry['label'])}"
        f"</a></li>"
        for entry in entries
    )
    return (
        '<aside class="toc-sticky">'
        '<nav class="toc">'
        '<div class="toc-title">Contents</div>'
        f"<ol>{items}</ol>"
        "</nav>"
        "</aside>"
    )


def _build_hero(
    *,
    pack: str,
    world: str | None,
    world_dir: Path | None,
) -> str:
    """Render the 5-element hero block (Story 63-7 Task B).

    Structure (per plan lines 2674-2685):

    ```html
    <header class="hero" id="hero">
      <div class="hero-eyebrow">
        <span class="glyph">…</span>
        <span class="eyebrow gilt">SideQuest · {label} · World Reference</span>
        <span class="rule"></span>
      </div>
      <div class="hero-kicker">{kicker}</div>
      <h1 class="hero-title">{title}</h1>
      <div class="hero-sub">{label} · Lore &amp; Rules</div>
      <div class="hero-epigraph narrative-flourish">
        {body}<span class="attrib">{attrib}</span>
      </div>
    </header>
    ```

    Title resolution:
    - Lore page (``world_dir`` provided): read ``world_dir/lore.yaml``
      and use ``world_name``. If absent or empty, fall back to
      ``PACK_LABELS[pack]`` and fire ``reference_hero_unbound_span``
      (WARN).
    - Rules page (``world_dir`` is None): title is ``PACK_LABELS[pack]``.

    Kicker resolution: first sentence of ``lore.world.description`` if
    present (lore page), else ``PACK_BLURBS[pack]`` (falls back to empty
    string for unknown packs — silent because the toc_missing span
    already covers the broader gap; emitting two spans for one cause is
    noise).

    Glyph: pulled from the loaded ``ReferenceTheme.dinkus_medium``.
    """
    label = PACK_LABELS.get(pack, pack)
    blurb = PACK_BLURBS.get(pack, "")
    epigraph = PACK_EPIGRAPHS.get(pack, {"body": "", "attrib": ""})

    # Default values; possibly overridden by lore.yaml on lore page.
    title = label
    kicker = blurb

    # Lore-page hero reads lore.yaml for world_name and description override.
    if world_dir is not None and world is not None:
        lore_path = world_dir / "lore.yaml"
        if lore_path.is_file():
            with lore_path.open(encoding="utf-8") as fh:
                lore_data = yaml.safe_load(fh) or {}
            world_name = lore_data.get("world_name")
            if world_name:
                title = str(world_name)
            else:
                # Hero falls back to pack label; fire WARN span so the GM
                # panel surfaces world-binding drift.
                with reference_hero_unbound_span(pack=pack, world=world):
                    pass
            world_desc = ((lore_data.get("world") or {}).get("description")) or lore_data.get(
                "description"
            )
            if world_desc:
                first_sentence = str(world_desc).split(".")[0].strip()
                if first_sentence:
                    kicker = first_sentence + "."
        else:
            with reference_hero_unbound_span(pack=pack, world=world):
                pass

    # The dinkus glyph is per-pack from theme.yaml — caller passes it via
    # ReferenceTheme. We read it from the loaded theme at the page-
    # assembler level so this function can be called with or without a
    # full theme (e.g. tests).
    return _hero_html(
        title=title,
        label=label,
        kicker=kicker,
        epigraph=epigraph,
    )


def _hero_html(
    *,
    title: str,
    label: str,
    kicker: str,
    epigraph: dict[str, str],
) -> str:
    """Pure HTML assembly for the 5-element hero. Every interpolation is
    escaped — the inputs may have come from author-controlled YAML.
    """
    return (
        '<header class="hero" id="hero">'
        '<div class="hero-eyebrow">'
        # The glyph slot is filled by the bundle's CSS via
        # `[data-archetype]` rules; an empty `.glyph` span lets the
        # bundle drop in its archetype-specific ornament. We keep it
        # empty here because the per-pack dinkus already varies through
        # the inline `:root{--ref-*}` style block.
        '<span class="glyph"></span>'
        f'<span class="eyebrow gilt">SideQuest · {escape(label)} · World Reference</span>'
        '<span class="rule"></span>'
        "</div>"
        f'<div class="hero-kicker">{escape(kicker)}</div>'
        f'<h1 class="hero-title">{escape(title)}</h1>'
        f'<div class="hero-sub">{escape(label)} · Lore &amp; Rules</div>'
        '<div class="hero-epigraph narrative-flourish">'
        f"{escape(epigraph.get('body', ''))}"
        f'<span class="attrib">{escape(epigraph.get("attrib", ""))}</span>'
        "</div>"
        "</header>"
    )


# --- Section walk by TOC id ---


def _file_renders_by_stem(
    files: tuple[str, ...],
    base_dir: Path,
    *,
    pack: str,
    world: str | None,
    theme: ReferenceTheme,
    label_suffix: str = "",
    poi_image_slugs: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Render every existing file from ``files`` in ``base_dir``, keyed by
    stem (without the ``.yaml`` extension)."""
    out: dict[str, str] = {}
    for filename in files:
        if filename in EXCLUDED_FILES:
            continue
        path = base_dir / filename
        if not path.exists():
            continue
        if label_suffix:
            rendered = _render_file_with_label(
                path, label_suffix, pack=pack, world=world, theme=theme
            )
        else:
            rendered = _render_file(
                path, pack=pack, world=world, theme=theme, poi_image_slugs=poi_image_slugs
            )
        if rendered:
            stem = path.stem
            # Two files with the same stem (e.g. pack-flavor cultures.yaml
            # AND world cultures.yaml) get concatenated under the same
            # key so the section renders both.
            if stem in out:
                out[stem] = out[stem] + rendered
            else:
                out[stem] = rendered
    return out


def _wrap_sections_by_toc(
    pack: str,
    rendered_by_stem: dict[str, str],
    *,
    toc_entries: list[dict[str, str]] | None = None,
) -> str:
    """Bucket the rendered file fragments into per-TOC-id sections.

    For each TOC entry of ``pack``, emit
    ``<section id="{toc.id}">{concatenated stems}</section>`` — even if
    no mapped files exist (the TOC link must resolve, per AC4 + Task D).

    Stems not referenced by any TOC entry render afterwards in their
    existing per-file wrappers so content is never silently dropped.
    """
    entries = toc_entries if toc_entries is not None else _pack_toc_entries(pack)
    used_stems: set[str] = set()
    parts: list[str] = []
    for entry in entries:
        toc_id = entry["id"]
        stems = TOC_TO_FILES.get(toc_id, [])
        section_body_parts: list[str] = []
        for stem in stems:
            if stem in rendered_by_stem:
                section_body_parts.append(rendered_by_stem[stem])
                used_stems.add(stem)
        section_body = "".join(section_body_parts)
        # Emit the section wrapper even when empty so the TOC anchor
        # resolves (a missing anchor would bring up the bad-anchor banner).
        parts.append(f'<section id="{escape(toc_id)}">{section_body}</section>')

    # Append unmapped stems at the end so their content is reachable.
    for stem, rendered in rendered_by_stem.items():
        if stem not in used_stems:
            parts.append(rendered)
    return "".join(parts)


# --- Top-level document wrap ---


def _wrap_document(
    *,
    title: str,
    body: str,
    pack: str,
    theme: ReferenceTheme,
    world: str | None = None,
    hero_html: str = "",
    toc_entries: list[dict[str, str]] | None = None,
) -> str:
    """Assemble the final HTML document.

    Layout (Story 63-7 Tasks A, C):

    ```
    <body>
      {bad-anchor banner}
      {ref-anchors island}
      {bad-anchor script}
      <div class="page">
        {hero_html}
        <div class="layout">
          {_build_toc(pack)}
          <main>{body}</main>
        </div>
      </div>
      {_SCROLL_SPY_SCRIPT}    ← intentionally OUTSIDE .page wrapper
    </body>
    ```

    The scroll-spy script stays outside ``.page`` for parser-
    friendliness — the bundle's CSS doesn't care, and keeping inline
    scripts at body root sidesteps any odd interaction with future
    ``.page`` rules.
    """
    anchors = _collect_anchor_ids(hero_html + body)
    island = f'<script id="ref-anchors" type="application/json">{json.dumps(anchors)}</script>'
    toc = _build_toc(pack, toc_entries=toc_entries)
    return (
        "<!doctype html>"
        f"{_document_root_open(pack=pack, world=world, archetype=theme.archetype)}"
        "<head>"
        '<meta charset="utf-8">'
        f"<title>{escape(title)}</title>"
        '<link rel="stylesheet" href="/reference/static/theme.css">'
        '<link rel="stylesheet" href="/reference/static/styles.css">'
        '<link rel="stylesheet" href="/reference/static/presenters.css">'
        '<script defer src="/reference/static/islands.js"></script>'
        f"{_theme_style_block(theme)}"
        "</head>"
        "<body>"
        f"{_BAD_ANCHOR_BANNER}"
        f"{island}"
        f"{_BAD_ANCHOR_SCRIPT}"
        '<div class="page">'
        f"{hero_html}"
        '<div class="layout">'
        f"{toc}"
        f"<main>{body}</main>"
        "</div>"
        "</div>"
        f"{_SCROLL_SPY_SCRIPT}"
        "</body>"
        "</html>"
    )


def assemble_rules_page(pack: str, pack_dir: Path) -> str:
    """Build the /reference/rules/<pack> HTML document.

    Rules pages render every existing file in ``RULES_FILES`` and wrap
    the renders by TOC id per ``TOC_TO_FILES``. Hero title is
    ``PACK_LABELS[pack]`` (no lore.yaml at the pack tier).
    """
    theme = load_reference_theme(pack_dir)
    rules_toc = list(DEFAULT_RULES_TOC)
    rendered_by_stem = _file_renders_by_stem(
        RULES_FILES, pack_dir, pack=pack, world=None, theme=theme
    )
    body = _wrap_sections_by_toc(pack, rendered_by_stem, toc_entries=rules_toc)
    hero_html = _build_hero(pack=pack, world=None, world_dir=None)
    return _wrap_document(
        title=f"{pack} — Rules",
        body=body,
        pack=pack,
        theme=theme,
        hero_html=hero_html,
        toc_entries=rules_toc,
    )


def load_poi_image_slugs(world_dir: Path) -> frozenset[str]:
    """Story 63-8: the set of location slugs that have a generated POI
    landscape image.

    The manifest is ``history.yaml`` ``points_of_interest[].slug`` (under
    ``chapters[]`` and/or top-level). Slugs are ``slugify``-normalised so they
    match the ``location-{slug}`` card ids the geography presenter emits — the
    authored POI slug (often underscore-style) and the card slug (hyphenated)
    both pass through ``slugify``."""
    path = world_dir / "history.yaml"
    if not path.exists():
        return frozenset()
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"history.yaml: malformed YAML: {exc}") from exc
    if not isinstance(data, dict):
        return frozenset()
    pois: list[object] = []
    chapters = data.get("chapters")
    if isinstance(chapters, list):
        for chapter in chapters:
            if isinstance(chapter, dict) and isinstance(chapter.get("points_of_interest"), list):
                pois.extend(chapter["points_of_interest"])
    if isinstance(data.get("points_of_interest"), list):
        pois.extend(data["points_of_interest"])
    slugs: set[str] = set()
    for poi in pois:
        if not isinstance(poi, dict):
            continue
        raw = poi.get("slug") or poi.get("name")
        if raw and (normalised := slugify(str(raw))):
            slugs.add(normalised)
    return frozenset(slugs)


def assemble_lore_page(pack: str, world: str, pack_dir: Path, world_dir: Path) -> str:
    """Build the /reference/lore/<pack>/<world> HTML document.

    Lore pages render the world-tier files (``LORE_WORLD_FILES`` from
    ``world_dir``) plus the pack-tier flavor files (``LORE_PACK_FLAVOR_FILES``
    from ``pack_dir``). Pack-flavor renders get the ``(genre)`` label
    suffix so authors can tell at a glance which tier the content
    came from.

    Hero title is the world's ``world_name`` from ``world_dir/lore.yaml``,
    falling back to ``PACK_LABELS[pack]`` with a WARN span.
    """
    theme = load_reference_theme(pack_dir)
    hero_html = _build_hero(pack=pack, world=world, world_dir=world_dir)

    world_rendered = _file_renders_by_stem(
        LORE_WORLD_FILES,
        world_dir,
        pack=pack,
        world=world,
        theme=theme,
        poi_image_slugs=load_poi_image_slugs(world_dir),
    )
    flavor_rendered = _file_renders_by_stem(
        LORE_PACK_FLAVOR_FILES,
        pack_dir,
        pack=pack,
        world=world,
        theme=theme,
        label_suffix="(genre)",
    )
    # Merge: pack-flavor renders come AFTER same-stem world renders so
    # world-tier content takes precedence in source order.
    merged: dict[str, str] = dict(world_rendered)
    for stem, rendered in flavor_rendered.items():
        if stem in merged:
            merged[stem] = merged[stem] + rendered
        else:
            merged[stem] = rendered

    body = _wrap_sections_by_toc(pack, merged)
    return _wrap_document(
        title=f"{pack} / {world} — Lore",
        body=body,
        pack=pack,
        theme=theme,
        world=world,
        hero_html=hero_html,
    )
