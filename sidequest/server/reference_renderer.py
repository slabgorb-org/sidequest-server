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
from collections.abc import Collection
from functools import lru_cache
from html import escape
from pathlib import Path

import yaml

from sidequest.game import npc_pool
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.server.reference_map import load_cartography_config, present_lore_map
from sidequest.server.reference_presenters import (
    PresenterContext,
    cast_portrait_slug,
    lookup_presenter,
    poi_image_key,
    portrait_image_key,
    present_lore_cast,
    present_renderable_landscapes,
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
from sidequest.server.reference_timeline import (
    load_legends,
    load_lore_history,
    present_lore_timeline,
)
from sidequest.server.reference_visibility import Visibility, classify
from sidequest.server.utils import slugify_player_name
from sidequest.telemetry.spans.reference import (
    reference_devnote_suppressed_span,
    reference_hero_unbound_span,
    reference_lore_assembled_span,
    reference_lore_section_orphaned_span,
    reference_manifest_loaded_span,
    reference_npc_unratified_skipped_span,
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
    ``_DEPTH_CAP`` for a non-empty container, the subtree is SUPPRESSED (emits
    nothing) rather than dumped as raw YAML/dict text — a player reference page
    must never leak a config blob (ADR-135). The suppression fires the
    unknown-field OTEL span (when ``ctx`` is present) so the GM panel sees it.

    ``kind`` is threaded from ``_render_file`` to namespace list-of-dict anchor
    ids. Top-level dict keys and recursively nested keys always use flat slugs;
    only the direct children of a top-level list are namespaced.

    ``ctx`` carries the presenter context (pack, world, file_stem, key_path,
    theme, depth) for visibility classification and presenter dispatch. When
    None (e.g. synthetic callers), classification is skipped and all fields
    render via the generic fallback.
    """
    if depth >= _DEPTH_CAP and isinstance(node, (dict, list)) and node:
        # A player reference page must NEVER emit a raw YAML/dict re-dump
        # (ADR-135 — reference pages are a clean table tool, not a config
        # dump). The live glenross bug: a `time_precision: {registers: {...}}`
        # blob deeper than the cap landed verbatim on the lore page. Suppress
        # the over-cap subtree instead of str()-dumping it. Loud-drop via the
        # existing unknown-field observability so the GM panel still sees the
        # suppression (No Silent Fallbacks).
        if ctx is not None:
            with reference_unknown_field_span(
                pack=ctx.pack,
                world=ctx.world,
                file_stem=ctx.file_stem,
                key_path=ctx.key_path,
            ):
                pass
            _log_unknown_once(ctx.file_stem, ctx.key_path)
        return ""
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

    Conservative by design:

    - A string that already contains whitespace is assumed author-formatted and
      returned UNCHANGED ("Floor It", "Item 3").
    - A string carrying an underscore or hyphen is a developer identifier; it is
      ALWAYS split and Title-Cased so the raw separator can never survive into a
      heading ("the_maw" → "The Maw", "MECHANICAL_surface" → "Mechanical
      Surface", "tier-1" → "Tier 1"). Story 63-9 AC1: no raw "_" reaches HTML.
    - A *separator-free* token with any uppercase letter is an acronym or
      proper noun and returned UNCHANGED ("USB", "McGuffin"). NOTE: this only
      protects a whole-string acronym. An acronym embedded in a compound
      identifier key ("USB_port") still splits and Title-Cases each part
      ("Usb Port") — the AC1 safe invariant (no raw "_" in HTML) wins over
      acronym fidelity, since a snake_case key is a developer string first.
    - A bare lowercase word is Title-Cased ("setting" → "Setting").
    """
    text = str(raw).strip()
    if not text:
        return text
    if any(c.isspace() for c in text):
        return text
    if "_" in text or "-" in text:
        parts = [p for p in re.split(r"[_\-]+", text) if p]
        return " ".join(p.capitalize() for p in parts) or text
    if any(c.isupper() for c in text):
        return text
    return text.capitalize()


# Story 63-9: leading-token markers that flag developer/placeholder prose. A
# marker only suppresses when it is the leading token of the value (a token
# boundary follows), so legitimate in-world prose like "a list of todos" is
# never eaten. TEA-derived contract — do not widen without Architect sign-off.
_DEVNOTE_MARKERS: tuple[str, ...] = ("TODO", "FIXME", "XXX", "PLACEHOLDER", "DEV NOTE")


def _is_devnote(value: object) -> bool:
    """True when ``value`` is a string whose leading token is a dev-note marker."""
    if not isinstance(value, str):
        return False
    # Collapse internal whitespace runs (double space, tab, NBSP) to a single
    # space so multi-word markers like "DEV  NOTE" / "DEV\tNOTE" still match.
    normalized = re.sub(r"\s+", " ", value).strip()
    upper = normalized.upper()
    for marker in _DEVNOTE_MARKERS:
        if upper.startswith(marker):
            rest = normalized[len(marker) :]
            # Leading token only — the marker must be followed by a boundary,
            # not be the prefix of a longer word ("todos" must not match TODO).
            if not rest or not (rest[0].isalnum() or rest[0] == "_"):
                return True
    return False


def _scalar_text(value: object) -> str:
    """Humanize a leaf scalar to display text. Bools become Yes/No so raw
    ``True``/``False`` never reach the reader (Story 63-9 AC3); ``None`` becomes
    the same ``(none)`` placeholder ``_render_scalar`` uses, never bare
    ``None``."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return "<em>(none)</em>"
    return escape(str(value))


def _render_scalar(value: object) -> str:
    if value is None:
        return "<p><em>(none)</em></p>"
    if isinstance(value, bool):
        return f"<p>{'Yes' if value else 'No'}</p>"
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

        # Story 63-9 humanization guard. Suppress private (leading-underscore)
        # keys and dev-note/placeholder values from the player-/author-facing
        # walk. Lives here in the fallback walk — NOT in reference_visibility,
        # whose stem-default for lore/rules is PUBLIC (TEA finding). Loud drop:
        # fires an OTEL span so the GM panel sees the suppression, never silent.
        if ctx is not None and (str(key).startswith("_") or _is_devnote(value)):
            with reference_devnote_suppressed_span(
                pack=ctx.pack,
                world=ctx.world,
                file_stem=ctx.file_stem,
                key_path=child_path,
            ):
                pass
            continue

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
        lis: list[str] = []
        for item in items:
            # Story 63-9: dev-note markers leak through scalar-LIST items too,
            # not just scalar dict values. Suppress here (ctx-bearing walk) and
            # fire the same span as the dict path — a dropped list item is the
            # same loud author-content decision (No Silent Fallbacks).
            if ctx is not None and _is_devnote(item):
                with reference_devnote_suppressed_span(
                    pack=ctx.pack,
                    world=ctx.world,
                    file_stem=ctx.file_stem,
                    key_path=ctx.key_path,
                ):
                    pass
                continue
            lis.append(f"<li>{_scalar_text(item)}</li>")
        return f"<ul>{''.join(lis)}</ul>"
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


# Generic-walk results that carry no player-facing content. render_node emits
# these for empty containers / empty files; an all-KEEPER-dropped dict yields "".
_EMPTY_BODY_PLACEHOLDERS: frozenset[str] = frozenset(
    {"<p><em>(empty)</em></p>", "<p><em>(empty file)</em></p>"}
)


def _body_has_content(body: str) -> bool:
    """True when a rendered file body carries real player-facing content.

    False for whitespace-only output and for the generic walk's "(empty)" /
    "(empty file)" placeholders — the signals Story 63-11 suppresses so an
    empty section (and its TOC entry) is dropped rather than rendered hollow.
    """
    stripped = body.strip()
    return bool(stripped) and stripped not in _EMPTY_BODY_PLACEHOLDERS


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
            # Presenter returned empty — it didn't recognise this data shape,
            # NOT "intentionally suppress". Fall through to the generic walk so
            # content the presenter can't render (e.g. a beat_vocabulary's
            # event_flavor / decision_framings / chase_modes, which
            # present_beat_vocabulary skips) is still surfaced, never
            # blackholed. Genuinely-empty data is caught by the body check below.

    body = "<p><em>(empty file)</em></p>" if data is None else render_node(data, kind=kind, ctx=ctx)
    # Story 63-11: present-but-empty data renders only a placeholder / nothing —
    # achievements: [] -> "(empty)"; a beat_vocab carrying only the
    # KEEPER-dropped `obstacles` key -> ""; an empty file -> "(empty file)".
    # Suppress it (return "") so _file_renders_by_stem omits the stem and
    # _wrap_sections_by_toc drops both the hollow <section> and its dangling TOC
    # link. Real content (any other markup) still wraps and renders.
    if not _body_has_content(body):
        return ""
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
) -> tuple[str, list[dict[str, str]]]:
    """Bucket the rendered file fragments into per-TOC-id sections.

    For each TOC entry of ``pack`` whose mapped stems produced content, emit
    ``<section id="{toc.id}">{concatenated stems}</section>``.

    Story 63-11: a TOC entry whose mapped stems render NOTHING (absent file,
    or a presenter that suppressed present-but-empty data) is dropped — both
    the empty ``<section>`` AND the entry itself — so the caller can omit the
    matching nav link. A dangling link to an empty anchor previously tripped
    the bad-anchor banner and showed a placeholder section.

    Returns ``(body_html, kept_entries)`` where ``kept_entries`` is the subset
    of TOC entries that actually rendered a section, in order. Stems not
    referenced by any TOC entry still render afterwards in their existing
    per-file wrappers so content is never silently dropped.
    """
    entries = toc_entries if toc_entries is not None else _pack_toc_entries(pack)
    used_stems: set[str] = set()
    parts: list[str] = []
    kept_entries: list[dict[str, str]] = []
    for entry in entries:
        toc_id = entry["id"]
        stems = TOC_TO_FILES.get(toc_id, [])
        section_body_parts: list[str] = []
        for stem in stems:
            if stem in rendered_by_stem:
                section_body_parts.append(rendered_by_stem[stem])
                used_stems.add(stem)
        section_body = "".join(section_body_parts)
        if not section_body:
            # Empty section: drop it and its TOC entry (Story 63-11).
            continue
        parts.append(f'<section id="{escape(toc_id)}">{section_body}</section>')
        kept_entries.append(entry)

    # Append unmapped stems at the end so their content is reachable.
    for stem, rendered in rendered_by_stem.items():
        if stem not in used_stems:
            parts.append(rendered)
    return "".join(parts), kept_entries


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
    body, kept_toc = _wrap_sections_by_toc(pack, rendered_by_stem, toc_entries=rules_toc)
    hero_html = _build_hero(pack=pack, world=None, world_dir=None)
    return _wrap_document(
        title=f"{pack} — Rules",
        body=body,
        pack=pack,
        theme=theme,
        hero_html=hero_html,
        toc_entries=kept_toc,
    )


def load_points_of_interest(world_dir: Path) -> list[dict]:
    """Every ``points_of_interest`` dict authored in ``history.yaml`` (under
    ``chapters[]`` and/or top-level), in authored order.

    This is the single source shared by ``load_poi_image_slugs`` (which derives
    the R2-gate slug set) and ``present_renderable_landscapes`` (which renders
    the POIs as landscape cards). The POIs carry ``name``/``slug``/``region``/
    ``type``/``description`` — the same shape the legacy geography presenter
    wanted, but authored here in history.yaml, where every live world actually
    puts them (no world ships a geography.yaml/locations.yaml)."""
    path = world_dir / "history.yaml"
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"history.yaml: malformed YAML: {exc}") from exc
    if not isinstance(data, dict):
        return []
    pois: list[dict] = []
    chapters = data.get("chapters")
    if isinstance(chapters, list):
        for chapter in chapters:
            if isinstance(chapter, dict) and isinstance(chapter.get("points_of_interest"), list):
                pois.extend(p for p in chapter["points_of_interest"] if isinstance(p, dict))
    if isinstance(data.get("points_of_interest"), list):
        pois.extend(p for p in data["points_of_interest"] if isinstance(p, dict))
    return pois


def load_poi_slug_map(world_dir: Path) -> dict[str, str]:
    """Story 71-38: ``{anchor_slug: verbatim_slug}`` for each authored POI.

    The two forms are DISTINCT and serve different consumers:

    * **anchor_slug** = ``slugify(authored slug)`` (hyphen) — the
      ``location-{slug}`` / ``landscape-{slug}`` HTML card id and the
      ``/reference/lore#location-<slug>`` deep-link anchor
      (:func:`reference_url_for_region`). Conventional, blast-radius-bearing
      (Story 63-6).
    * **verbatim_slug** = the authored ``history.yaml`` ``points_of_interest[].slug``
      VERBATIM (often underscore) — the R2 object key, because
      ``render_common.py`` writes ``<slug>.png`` from the authored slug
      unchanged. This is the key :func:`poi_image_key` must be fed (NEVER the
      slugified form) so the manifest gate matches the asset that is actually on
      R2.

    Conflating the two (slugifying the authored slug and then feeding it to
    ``poi_image_key``) is the Story 71-38 bug: a hyphen R2 key never matches the
    underscore manifest key, so every underscore-slug world emitted 0 POI
    ``<img>``. Keyed on the anchor form so a later POI cannot silently overwrite
    an earlier one whose authored slug slugifies identically."""
    mapping: dict[str, str] = {}
    for poi in load_points_of_interest(world_dir):
        raw = poi.get("slug") or poi.get("name")
        if raw:
            verbatim = str(raw)
            if anchor := slugify(verbatim):
                mapping.setdefault(anchor, verbatim)
    return mapping


def load_poi_image_slugs(world_dir: Path) -> frozenset[str]:
    """Story 63-8 / 71-38: the set of POI **anchor** slugs (``slugify``-normalised,
    hyphen) — the form the card ids and ``/reference/lore#location-<slug>`` deep-link
    anchors use (:func:`reference_url_for_region`, which matches
    ``slugify(region_id)`` against this set).

    This is the **anchor** projection of :func:`load_poi_slug_map`. The R2-object-key
    gate (:func:`_gate_poi_slugs_on_manifest`) does NOT consume this set — it takes the
    full ``{anchor: verbatim}`` map, because the R2 key is the *verbatim* authored slug,
    not the slugified anchor (Story 71-38 decouple). Kept as the anchor-only frozenset so
    the deep-link consumer in ``map_emit`` is untouched."""
    return frozenset(load_poi_slug_map(world_dir))


@lru_cache(maxsize=8)
def load_r2_manifest_keys(manifest_path: Path) -> frozenset[str]:
    """Story 65-8: the set of R2 object keys recorded in ``r2_manifest.json``
    (the Story 65-7 existence oracle). Story 65-9 reuses it for portrait keys.

    Used to gate POI and Cast-portrait ``<img>`` emission on the lore page so an
    authored-but-not-rendered asset never produces a broken image.

    **Caching / staleness (runbook):** ``@lru_cache(maxsize=8)`` keyed by the
    manifest ``Path`` — the result is computed once per distinct path and held
    for the process (up to 8 paths, then LRU-evicted). There is **no TTL and no
    mtime check**: a manifest regenerated after an asset upload is NOT picked up
    until the server restarts (or the cache is cleared via
    ``load_r2_manifest_keys.cache_clear()``). This is safe-failing — a just-
    rendered asset missing from a stale cached manifest renders text-only, never
    broken — but operators must restart the server after regenerating
    ``r2_manifest.json`` for new art to appear on the reference page.

    Fails loud — never returns a silently-empty set on error (No Silent
    Fallbacks): an absent file raises ``FileNotFoundError``; malformed JSON or a
    wrong-shape manifest (not a list, or an entry missing ``key``) raises
    ``ValueError``.
    """
    with manifest_path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(
            f"r2_manifest.json: expected a JSON array of entries, got "
            f"{type(data).__name__}: {manifest_path}"
        )
    keys: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict) or "key" not in entry:
            raise ValueError(f"r2_manifest.json: malformed entry (missing 'key'): {manifest_path}")
        keys.add(str(entry["key"]))
    return frozenset(keys)


def _gate_poi_slugs_on_manifest(
    slug_map: dict[str, str],
    *,
    pack: str,
    world: str,
    pack_dir: Path,
) -> frozenset[str]:
    """Story 65-8 / 71-38: filter authored POIs to those whose R2 landscape image
    is actually present in ``r2_manifest.json``, returning the surviving **anchor**
    slugs. Authored-but-not-on-R2 POIs render text-only — no broken ``<img>``.

    Takes the ``{anchor_slug: verbatim_slug}`` map from :func:`load_poi_slug_map`.
    The manifest comparison feeds :func:`poi_image_key` the **verbatim** slug (the
    real R2 object key), while the returned set is the **anchor** (slugify) form the
    presenters key cards on — the Story 71-38 decouple. Feeding ``poi_image_key`` the
    anchor form was the bug: a hyphen key never matched an underscore manifest key.

    The manifest is required only when the world authors POIs; a POI-less lore
    page never consults it (so the manifest is not a hard dependency of every
    render). For a POI-bearing world, an absent/malformed manifest is a loud
    failure surfaced by :func:`load_r2_manifest_keys`. The manifest lives at the
    content root — ``pack_dir.parent.parent`` (prod:
    ``sidequest-content/r2_manifest.json``).

    Emits one ``reference_manifest_loaded`` span per render **when the world
    authors POIs** — a POI-less world short-circuits and emits no span, so the
    span count tracks feature-bearing renders, not every render.
    """
    if not slug_map:
        return frozenset()
    manifest_path = pack_dir.parent.parent / "r2_manifest.json"
    manifest_keys = load_r2_manifest_keys(manifest_path)
    world_prefix = f"genre_packs/{pack}/worlds/{world}/assets/poi/"
    world_key_count = sum(1 for key in manifest_keys if key.startswith(world_prefix))
    with reference_manifest_loaded_span(
        path=str(manifest_path),
        entry_count=len(manifest_keys),
        world_key_count=world_key_count,
    ):
        pass
    return frozenset(
        anchor
        for anchor, verbatim in slug_map.items()
        if poi_image_key(pack, world, verbatim) in manifest_keys
    )


def load_cast_entries(world_dir: Path) -> list[dict]:
    """Story 65-9: the public Cast projection — characters from
    ``portrait_manifest.yaml`` (the same file Story 65-6 reads for scene-time
    portraits). Returns ``[]`` when the world authors no manifest, so the caller
    omits the Cast section. Keeper-only ``npcs.yaml`` is never read here.

    Accepts either of the two top-level shapes the genre loader also tolerates: a
    ``{characters: [...]}`` mapping or a bare list. That two-shape tolerance is the
    only behavior shared with the genre loader — unlike the genre loader's
    ``_load_portrait_manifest`` (which ``model_validate``s each entry and would
    *raise* on a non-dict), the non-dict-item drop below is local to this function.
    A non-list ``characters:`` value is malformed first-party authoring and fails
    loud with a ``ValueError`` (No Silent Fallbacks), never an uncaught
    ``TypeError`` from iterating a scalar (Story 65-13).
    """
    path = world_dir / "portrait_manifest.yaml"
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"portrait_manifest.yaml: malformed YAML: {exc}") from exc
    if isinstance(data, dict):
        chars = data.get("characters", [])
    elif isinstance(data, list):
        chars = data
    else:
        chars = []
    if not isinstance(chars, list):
        raise ValueError(
            f"portrait_manifest.yaml: 'characters' must be a list, got "
            f"{type(chars).__name__}: {path}"
        )
    return [c for c in chars if isinstance(c, dict)]


def _cast_entry_is_projectable(entry: dict) -> bool:
    """ADR-138 §D4 ratification gate for a public Cast entry.

    A ``portrait_manifest.yaml`` entry is projectable onto the public reference
    page iff it is **ratified** — exactly the rule the ADR-118 retrieval index
    applies (75-12). Reuses the 75-11 single-source predicate
    (:func:`sidequest.game.npc_pool.is_projectable`) rather than re-deriving the
    rule: the YAML dict is adapted to a minimal :class:`NpcPoolMember` carrying
    only the gate-relevant fields (the manifest also carries author-facing keys
    like ``role``/``appearance``/``id`` that ``NpcPoolMember`` forbids, so the
    whole dict cannot be splatted in).

    Authored manifest content is never auto-minted, so ``observation_pending`` is
    ``False`` by design and this returns ``True`` in practice; the gate exists
    defensively so a future unratified entry cannot leak a phantom onto a
    player-facing page (No Silent Fallbacks — the skip is counted on an OTEL span,
    never silently rendered).

    The raw ``observation_pending`` value is handed to ``NpcPoolMember`` for
    Pydantic coercion rather than pre-wrapped in ``bool()``: ``bool("false")`` is
    ``True`` (every non-empty string is truthy), so a quoted-string authoring slip
    (``observation_pending: "false"``) would otherwise *silently withhold a
    ratified NPC*. Pydantic v2 coerces ``"false"``/``"true"``/``0``/``1`` to the
    correct bool and raises loudly on unsalvageable input. An explicit ``null``
    (Python ``None``) is coalesced to the ``False`` default — ``null`` means
    "unset", identical to an absent key, and must render rather than raise a
    ValidationError that would 500 the public page."""
    raw_pending = entry.get("observation_pending", False)
    member = NpcPoolMember(
        name=str(entry.get("name", "")),
        drawn_from="world_authored",
        observation_pending=False if raw_pending is None else raw_pending,
    )
    return npc_pool.is_projectable(member)


def _gate_cast_slugs_on_manifest(
    authored_slugs: frozenset[str],
    *,
    pack: str,
    world: str,
    pack_dir: Path,
) -> frozenset[str]:
    """Story 65-9: filter authored Cast portrait slugs to those whose R2 portrait
    image is present in ``r2_manifest.json``. Authored-but-not-on-R2 NPCs render
    text-only — no broken ``<img>``. Portrait analog of
    :func:`_gate_poi_slugs_on_manifest`; same loaded-once manifest, same
    ``pack_dir.parent.parent`` discovery, same loud-failure contract.

    Emits one ``reference_manifest_loaded`` span when the world authors cast
    NPCs; a cast-less world short-circuits and emits no span.
    """
    if not authored_slugs:
        return frozenset()
    manifest_path = pack_dir.parent.parent / "r2_manifest.json"
    manifest_keys = load_r2_manifest_keys(manifest_path)
    world_prefix = f"genre_packs/{pack}/worlds/{world}/assets/portraits/"
    world_key_count = sum(1 for key in manifest_keys if key.startswith(world_prefix))
    with reference_manifest_loaded_span(
        path=str(manifest_path),
        entry_count=len(manifest_keys),
        world_key_count=world_key_count,
    ):
        pass
    return frozenset(
        slug for slug in authored_slugs if portrait_image_key(pack, world, slug) in manifest_keys
    )


_ROMAN_NUMERALS: tuple[tuple[int, str], ...] = (
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


def _int_to_roman(n: int) -> str:
    """A TOC ordinal as a Roman numeral (matches DEFAULT_TOC/PACK_TOC ``num``).

    Used to number a dynamically-appended section (Story 65-9 Cast) so its TOC
    entry carries the ``num`` field ``_build_toc`` requires. Bounded by realistic
    section counts (well under the ``C`` ceiling)."""
    out: list[str] = []
    for value, symbol in _ROMAN_NUMERALS:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


# The closed set of dynamically-appended lore sections (Story 65-9/65-11/65-12).
# Used to derive ``lore_dynamic_sections`` for the assembly span (Story 65-10).
_DYNAMIC_SECTION_IDS = ("cast", "map", "timeline")


def _append_dynamic_section(
    body: str,
    kept_toc: list[dict[str, str]],
    *,
    section_id: str,
    label: str,
    html: str,
) -> tuple[str, list[dict[str, str]]]:
    """Append one dynamically-synthesized section + its TOC entry (Story 65-10).

    Unifies the three near-identical Cast/Map/Timeline append blocks: append the
    section HTML to ``body`` and append a numbered TOC entry (Roman numeral from
    the running TOC length) for ``section_id``. ``html`` must be non-empty — the
    caller guards on "did the presenter actually render anything".
    """
    return (
        body + html,
        [
            *kept_toc,
            {"num": _int_to_roman(len(kept_toc) + 1), "id": section_id, "label": label},
        ],
    )


def emit_lore_assembled_span(
    *,
    pack: str,
    world: str,
    toc_entries: list[dict[str, str]],
    anchor_ids: Collection[str],
) -> bool:
    """Record the composed lore-page TOC and check TOC<->section parity (65-10).

    Fires one ``sidequest.reference.lore_assembled`` span carrying the composed
    section ids, count, which dynamic sections registered, and ``parity_ok``.
    For every composed TOC id with no matching anchor in ``anchor_ids`` (a
    dangling nav link), fires one ``sidequest.reference.lore_section_orphaned``
    WARN span naming it — the server-side analog of the client bad-anchor banner.

    Returns ``parity_ok`` (``True`` iff every composed TOC id is anchored).
    """
    anchors = set(anchor_ids)
    section_ids = [entry["id"] for entry in toc_entries]
    missing = [sid for sid in section_ids if sid not in anchors]
    parity_ok = not missing
    dynamic = [sid for sid in section_ids if sid in _DYNAMIC_SECTION_IDS]

    with reference_lore_assembled_span(
        pack=pack,
        world=world,
        section_ids="/".join(section_ids),
        section_count=len(section_ids),
        dynamic_sections="/".join(dynamic),
        parity_ok=parity_ok,
    ):
        for sid in missing:
            with reference_lore_section_orphaned_span(pack=pack, world=world, section_id=sid):
                pass
    return parity_ok


def assemble_lore_page(pack: str, world: str, pack_dir: Path, world_dir: Path) -> str:
    """Build the /reference/lore/<pack>/<world> HTML document.

    Lore pages render the world-tier files only (``LORE_WORLD_FILES`` from
    ``world_dir``). Pack/genre-tier flavor is deliberately NOT merged in
    (Story 63-10, Architect-ratified absolute world-only): a world's lore can
    contradict its pack's cosmology, so concatenating pack flavor produced
    incoherent pages (e.g. beneath_sunden rejecting the Keeper/Maw cosmology
    its pack asserts). There is no ``(genre)`` tier label, since no genre-tier
    content appears.

    Hero title is the world's ``world_name`` from ``world_dir/lore.yaml``,
    falling back to ``PACK_LABELS[pack]`` with a WARN span.
    """
    theme = load_reference_theme(pack_dir)
    hero_html = _build_hero(pack=pack, world=world, world_dir=world_dir)

    # Story 65-8: gate authored POI slugs on R2 existence (r2_manifest.json) so
    # authored-but-not-rendered POIs render text-only instead of a broken <img>.
    # Story 71-38: gate on the {anchor: verbatim} map — the R2 key is the verbatim
    # authored slug, the returned survivors are anchor (slugify) form for the cards.
    poi_slug_map = load_poi_slug_map(world_dir)
    gated_poi_slugs = _gate_poi_slugs_on_manifest(
        poi_slug_map,
        pack=pack,
        world=world,
        pack_dir=pack_dir,
    )

    world_rendered = _file_renders_by_stem(
        LORE_WORLD_FILES,
        world_dir,
        pack=pack,
        world=world,
        theme=theme,
        poi_image_slugs=gated_poi_slugs,
    )

    body, kept_toc = _wrap_sections_by_toc(pack, world_rendered)

    # Story 65-9: public Cast section from portrait_manifest.yaml, with portrait
    # <img>s gated on R2 existence (the portrait analog of the POI gate above).
    cast_entries = load_cast_entries(world_dir)
    if cast_entries:
        # Story 75-13 (ADR-138 §D4): ratification gate. Withhold unratified
        # (observation_pending) phantoms from the public Cast for the same reason
        # 75-12 withholds them from the ADR-118 retrieval index — the world has
        # not committed to them. is_projectable() (75-11) is the shared single
        # source of truth. The count of withheld members is recorded on a
        # per-render span (fires even when 0) so the skip is observable, never
        # silent (No Silent Fallbacks); the count comes from the RAW authored
        # entries, so an all-unratified world still records the withholding even
        # though it renders no Cast section.
        ratified_entries = [e for e in cast_entries if _cast_entry_is_projectable(e)]
        with reference_npc_unratified_skipped_span(
            pack=pack,
            world=world,
            count=len(cast_entries) - len(ratified_entries),
        ):
            if ratified_entries:
                # Fix #4: gate on the SAME slug the presenter keys each card on —
                # the entry's explicit `id`, else slugify(name). Deriving the gated set
                # differently (e.g. always slugify(name)) would mismatch the per-card
                # key and silently drop every portrait whose id != slugify(name).
                authored_portrait_slugs = frozenset(
                    cast_portrait_slug(e)
                    for e in ratified_entries
                    if str(e.get("name", "")).strip()
                )
                gated_portrait_slugs = _gate_cast_slugs_on_manifest(
                    authored_portrait_slugs,
                    pack=pack,
                    world=world,
                    pack_dir=pack_dir,
                )
                cast_html = present_lore_cast(
                    ratified_entries,
                    pack=pack,
                    world=world,
                    theme=theme,
                    portrait_image_slugs=gated_portrait_slugs,
                )
                if cast_html:
                    body, kept_toc = _append_dynamic_section(
                        body, kept_toc, section_id="cast", label="Cast", html=cast_html
                    )

    # Story 65-11: public Map section — a server-rendered SVG node-link graph from
    # cartography.yaml, with npc-binding entity portraits gated on R2 the same way
    # the Cast portraits above are gated (reusing _gate_cast_slugs_on_manifest).
    cartography = load_cartography_config(world_dir)
    if cartography is not None and cartography.regions:
        map_npc_slugs = frozenset(
            slugify_player_name(ent.label)
            for region in cartography.regions.values()
            for ent in region.entities
            if ent.binding is not None and ent.binding.kind == "npc" and ent.label.strip()
        )
        gated_map_slugs = _gate_cast_slugs_on_manifest(
            map_npc_slugs,
            pack=pack,
            world=world,
            pack_dir=pack_dir,
        )
        map_html = present_lore_map(
            cartography,
            pack=pack,
            world=world,
            portrait_on_r2_slugs=gated_map_slugs,
        )
        if map_html:
            body, kept_toc = _append_dynamic_section(
                body, kept_toc, section_id="map", label="Map", html=map_html
            )

    # Renderable Landscapes — the world's history.yaml points_of_interest as a
    # landscape gallery, images gated on R2 via the SAME slug set the legacy
    # geography path used (gated_poi_slugs). This is the surface that actually
    # surfaces POI landscapes on the lore page: present_lore_geography only fires
    # for a geography.yaml/locations.yaml, which no live world authors, so POI
    # art never rendered despite being authored in history.yaml + on R2. Sits by
    # the Map section (both spatial) and self-omits when no POI art exists yet.
    pois = load_points_of_interest(world_dir)
    if pois:
        landscapes_html = present_renderable_landscapes(
            pois,
            pack=pack,
            world=world,
            theme=theme,
            poi_image_slugs=gated_poi_slugs,
        )
        if landscapes_html:
            body, kept_toc = _append_dynamic_section(
                body,
                kept_toc,
                section_id="landscapes",
                label="Renderable Landscapes",
                html=landscapes_html,
            )

    # Story 65-12: public world Timeline section — a world-historical spine from
    # the world's legends with an honest conditional sort (dated entries sorted
    # ascending only when uniformly parseable, else authored order). Reuses the
    # typed legend loader; emits no images, so no manifest gate is needed.
    legends = load_legends(world_dir)
    if legends:
        timeline_html = present_lore_timeline(
            legends,
            history_prose=load_lore_history(world_dir),
        )
        if timeline_html:
            body, kept_toc = _append_dynamic_section(
                body, kept_toc, section_id="timeline", label="Timeline", html=timeline_html
            )

    # Story 65-10: record the composed TOC + check TOC<->section parity once per
    # render, using the same anchor set _wrap_document ships to the client banner.
    emit_lore_assembled_span(
        pack=pack,
        world=world,
        toc_entries=kept_toc,
        anchor_ids=_collect_anchor_ids(hero_html + body),
    )

    return _wrap_document(
        title=f"{pack} / {world} — Lore",
        body=body,
        pack=pack,
        theme=theme,
        world=world,
        hero_html=hero_html,
        toc_entries=kept_toc,
    )
