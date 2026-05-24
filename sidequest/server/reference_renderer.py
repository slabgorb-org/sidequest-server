"""Render parsed-YAML trees as a hypertext document.

Pure functions only. The HTTP boundary (404 / 500 / file IO) lives in
reference_routes.py. This module just walks dict/list/scalar trees and produces
HTML fragments.

Headings get stable slugified ``id`` attributes so future deep-link work can
target them without schema changes.
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

import yaml

from sidequest.server.reference_slug import slugify
from sidequest.server.reference_theme import ReferenceTheme, load_reference_theme
from sidequest.telemetry.spans.reference import reference_hero_unbound_span

_DEPTH_CAP = 6

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
    "tropes": "trope",
}


def _kind_for_stem(stem: str) -> str:
    """Return the namespaced anchor kind for a given file stem.

    Default behaviour is to use the stem as-is (e.g. `history` → `history`),
    so non-plural files still benefit from file-level namespacing while
    pluralised files get a cleaner singular form.
    """
    return _KIND_OVERRIDES.get(stem, stem)


def render_node(node: object, depth: int = 0, kind: str | None = None) -> str:
    """Render a parsed-YAML node to an HTML fragment.

    Handles: dict (recursive nested <section>), list (ul for scalars, sectioned
    for dicts), scalar (str/int/float/bool/None). When ``depth`` reaches
    ``_DEPTH_CAP`` for a non-empty container, the subtree is dumped as YAML
    inside a ``<pre>`` block instead of recursing into runaway markup.

    ``kind`` is threaded from ``_render_file`` to namespace list-of-dict anchor
    ids. Top-level dict keys and recursively nested keys always use flat slugs;
    only the direct children of a top-level list are namespaced.
    """
    if depth >= _DEPTH_CAP and isinstance(node, (dict, list)) and node:
        dumped = yaml.safe_dump(node, sort_keys=False, default_flow_style=False)
        return f"<pre>{escape(dumped)}</pre>"
    if isinstance(node, dict):
        return _render_dict(node, depth, kind=kind) if node else "<p><em>(empty)</em></p>"
    if isinstance(node, list):
        return _render_list(node, depth, kind=kind) if node else "<p><em>(empty)</em></p>"
    return _render_scalar(node)


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
def _render_dict(node: dict, depth: int, kind: str | None = None) -> str:
    parts: list[str] = []
    for key, value in node.items():
        slug = slugify(str(key))
        # Dict keys always use flat slugs — only list-of-dict items are namespaced.
        parts.append(f'<section id="{slug}">')
        parts.append(f"<h2>{escape(str(key))}</h2>")
        # Forward kind so it reaches lists nested inside this dict's values.
        parts.append(render_node(value, depth + 1, kind=kind))
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
def _render_list(items: list, depth: int, kind: str | None = None) -> str:
    if all(not isinstance(item, (dict, list)) for item in items):
        lis = "".join(f"<li>{escape(str(item))}</li>" for item in items)
        return f"<ul>{lis}</ul>"
    parts: list[str] = []
    for index, item in enumerate(items):
        if isinstance(item, dict):
            slug, display = _heading_for_item(item, index, kind=kind)
            parts.append(f'<section id="{slug}">')
            parts.append(f"<h3>{escape(display)}</h3>")
            # Recursive render of the item's body uses no kind — nested dict keys
            # are flat-slugged per the spec (kind applies only to direct list items).
            parts.append(render_node(item, depth + 1))
            parts.append("</section>")
        else:
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
    "tropes.yaml",
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
)

EXCLUDED_FILES: frozenset[str] = frozenset(
    {
        # Spoiler-bearing — see iteration 2 of the spec
        "npcs.yaml",
        "seed_tropes.yaml",
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


# Inline IntersectionObserver scroll-spy — toggles aria-current on the
# contents-rail link whose target section is in view. Bounded ≤2KB so the
# guard test catches any accidental SPA-bundle inlining.
#
# rootMargin "-30% 0px -60% 0px" defines an "active band" between 30% and
# 40% from the top of the viewport: a section becomes active as it crosses
# the 30% line and stays active until it falls past 40%. This avoids the
# common scroll-spy flicker where two sections fight for active state when
# one ends and the next begins at exactly the same scroll position.
_SCROLL_SPY_SCRIPT = (
    "<script>"
    "(function(){"
    "var rail=document.querySelector('.contents-rail');"
    "if(!rail)return;"
    "var links=rail.querySelectorAll('a[href^=\"#\"]');"
    "var byId={};"
    "links.forEach(function(a){byId[a.getAttribute('href').slice(1)]=a;});"
    "var io=new IntersectionObserver(function(entries){"
    "entries.forEach(function(e){"
    "var a=byId[e.target.id];"
    "if(a&&e.isIntersecting){"
    "links.forEach(function(l){l.removeAttribute('aria-current');});"
    "a.setAttribute('aria-current','true');"
    "}"
    "});"
    "},{rootMargin:'-30% 0px -60% 0px'});"
    "Object.keys(byId).forEach(function(id){"
    "var el=document.getElementById(id);"
    "if(el)io.observe(el);"
    "});"
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


def _render_file(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name}: malformed YAML: {exc}") from exc
    kind = _kind_for_stem(path.stem)
    body = "<p><em>(empty file)</em></p>" if data is None else render_node(data, kind=kind)
    file_slug = slugify(path.stem)
    return (
        f'<section class="file" id="file-{file_slug}"><h1>{escape(path.name)}</h1>{body}</section>'
    )


def _render_file_with_label(path: Path, label: str) -> str:
    """Like _render_file but appends a parenthetical label to the file heading."""
    rendered = _render_file(path)
    if not rendered:
        return ""
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


def _build_contents_rail(entries: list[tuple[str, str]]) -> str:
    """Server-rendered locked TOC. ``data-scroll-spy`` marks it as the target
    for the inline IntersectionObserver script."""
    items = "".join(
        f'<li><a href="#{slug}">{escape(display)}</a></li>' for slug, display in entries
    )
    return f'<nav class="contents-rail" data-scroll-spy><ul>{items}</ul></nav>'


def _hero_fallback(pack: str, world: str) -> str:
    """Fallback hero: pack name only + WARN span. Shared between
    'lore.yaml absent' and 'world_name missing' paths in ``_build_hero``."""
    with reference_hero_unbound_span(pack=pack, world=world):
        return (
            '<header class="hero" id="hero">'
            f"<h1>{escape(pack)}</h1>"
            "</header>"
        )


def _build_hero(*, pack: str, world: str, world_dir: Path) -> str:
    """Lore-page hero block. Reads ``world_dir/lore.yaml``; falls back to the
    pack name + WARN span if lore.yaml is absent or has no ``world_name``."""
    lore_path = world_dir / "lore.yaml"
    if not lore_path.is_file():
        return _hero_fallback(pack, world)
    with lore_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    world_name = data.get("world_name")
    if not world_name:
        return _hero_fallback(pack, world)
    epigraph = data.get("epigraph") or ""
    epigraph_html = (
        f'<p class="epigraph">{escape(str(epigraph))}</p>' if epigraph else ""
    )
    return (
        '<header class="hero" id="hero">'
        f"<h1>{escape(str(world_name))}</h1>"
        f"{epigraph_html}"
        "</header>"
    )


def _wrap_document(
    *,
    title: str,
    body: str,
    pack: str,
    theme: ReferenceTheme,
    rail_entries: list[tuple[str, str]],
    world: str | None = None,
    hero_html: str = "",
) -> str:
    anchors = _collect_anchor_ids(hero_html + body)
    island = f'<script id="ref-anchors" type="application/json">{json.dumps(anchors)}</script>'
    rail = _build_contents_rail(rail_entries)
    return (
        "<!doctype html>"
        f"{_document_root_open(pack=pack, world=world, archetype=theme.archetype)}"
        "<head>"
        '<meta charset="utf-8">'
        f"<title>{escape(title)}</title>"
        f'<link rel="stylesheet" href="/reference/static/theme.css">'
        f'<link rel="stylesheet" href="/reference/static/styles.css">'
        f"{_theme_style_block(theme)}"
        "</head>"
        "<body>"
        f"{_BAD_ANCHOR_BANNER}"
        f"{island}"
        f"{_BAD_ANCHOR_SCRIPT}"
        f"{hero_html}"
        f"{rail}"
        f'<h1 class="doc-title">{escape(title)}</h1>'
        f"{body}"
        f"{_SCROLL_SPY_SCRIPT}"
        "</body>"
        "</html>"
    )


def _rail_entries_for(
    files: tuple[str, ...], base_dir: Path, *, label_suffix: str = ""
) -> tuple[list[tuple[str, str]], list[str]]:
    """Build rail entries + rendered file fragments for ``files`` in ``base_dir``,
    skipping any file that doesn't exist (parity with existing behavior)."""
    entries: list[tuple[str, str]] = []
    rendered: list[str] = []
    for filename in files:
        if filename in EXCLUDED_FILES:
            continue
        path = base_dir / filename
        if not path.exists():
            continue
        # Rail link targets must match the section wrapper id from
        # `_render_file`, which uses the ``file-{slug}`` prefix.
        file_id = f"file-{slugify(path.stem)}"
        if label_suffix:
            rendered.append(_render_file_with_label(path, label_suffix))
            entries.append((file_id, f"{path.name} {label_suffix}"))
        else:
            rendered.append(_render_file(path))
            entries.append((file_id, path.name))
    return entries, rendered


def assemble_rules_page(pack: str, pack_dir: Path) -> str:
    """Build the /reference/rules/<pack> HTML document."""
    theme = load_reference_theme(pack_dir)
    entries, rendered = _rail_entries_for(RULES_FILES, pack_dir)
    return _wrap_document(
        title=f"{pack} — Rules",
        body="".join(rendered),
        pack=pack,
        theme=theme,
        rail_entries=entries,
    )


def assemble_lore_page(pack: str, world: str, pack_dir: Path, world_dir: Path) -> str:
    """Build the /reference/lore/<pack>/<world> HTML document."""
    theme = load_reference_theme(pack_dir)
    hero_html = _build_hero(pack=pack, world=world, world_dir=world_dir)
    world_entries, world_rendered = _rail_entries_for(LORE_WORLD_FILES, world_dir)
    flavor_entries, flavor_rendered = _rail_entries_for(
        LORE_PACK_FLAVOR_FILES, pack_dir, label_suffix="(genre)"
    )
    rail_entries: list[tuple[str, str]] = [("hero", "Overview")]
    rail_entries.extend(world_entries)
    rail_entries.extend(flavor_entries)
    body = "".join(world_rendered) + "".join(flavor_rendered)
    return _wrap_document(
        title=f"{pack} / {world} — Lore",
        body=body,
        pack=pack,
        theme=theme,
        world=world,
        hero_html=hero_html,
        rail_entries=rail_entries,
    )
