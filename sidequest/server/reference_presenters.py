"""Per-section reference-page presenters.

Each presenter is a pure function: (node, PresenterContext) -> str (HTML).
The dispatcher in reference_renderer.py looks up (file_stem, key_path) in
PRESENTERS before falling back to the generic <h2>key</h2><p>value</p> loop.

Wildcard rule: ('*',) in the registry key matches any list-of-dict item slot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import escape

import yaml

from sidequest.server.asset_urls import resolve_asset_url
from sidequest.server.reference_slug import slugify
from sidequest.server.reference_theme import ReferenceTheme
from sidequest.telemetry.spans.reference import (
    reference_poi_image_not_found_span,
    reference_poi_image_resolved_span,
)

KeyPath = tuple[str, ...]


@dataclass(frozen=True)
class PresenterContext:
    pack: str
    world: str | None
    file_stem: str
    key_path: KeyPath
    theme: ReferenceTheme
    depth: int
    # Story 63-8: location slugs (already slugified) that have a generated POI
    # landscape image in R2. Built by ``assemble_lore_page`` from
    # ``history.yaml`` ``points_of_interest[].slug`` and threaded down through
    # the render walk. A location card emits an ``<img>`` iff its slug is here.
    poi_image_slugs: frozenset[str] = frozenset()


Presenter = Callable[[object, PresenterContext], str]


# Registry populated by subsequent tasks. Empty at Task 4 — dispatcher falls
# through to generic for every field, but visibility classification still runs.
PRESENTERS: dict[tuple[str, KeyPath], Presenter] = {}


def lookup_presenter(file_stem: str, key_path: KeyPath) -> Presenter | None:
    """Return the registered presenter or None.

    Exact match only. Callers are responsible for substituting "*" into
    list-of-dict slots in the key_path before calling.
    """
    return PRESENTERS.get((file_stem, key_path))


def present_world_name_suppress(node: object, ctx: PresenterContext) -> str:
    """Drop the duplicate world_name — already rendered as hero H1."""
    return ""


def present_lore_setting_anchor(node: object, ctx: PresenterContext) -> str:
    """Single narrative-flourish opening paragraph, no heading."""
    text = str(node).strip()
    if not text:
        return ""
    return f'<p class="narrative-flourish">{escape(text)}</p>'


def _split_paragraphs(prose: str) -> list[str]:
    return [p.strip() for p in str(prose).split("\n\n") if p.strip()]


def present_lore_history(node: object, ctx: PresenterContext) -> str:
    """Split history prose into paragraphs; first is pull-quoted with drop-cap;
    dinkus divider every 3-4 paragraphs."""
    paragraphs = _split_paragraphs(str(node))
    if not paragraphs:
        return ""
    parts: list[str] = ['<div class="ref-history">']

    # First paragraph: pull-quote with drop-cap
    first = paragraphs[0]
    if first:
        first_letter = first[0]
        remainder = first[1:]
        parts.append(
            '<p class="ref-pull-quote narrative-flourish">'
            f'<span class="ref-pull-quote__dropcap">{escape(first_letter)}</span>'
            f"{escape(remainder)}"
            "</p>"
        )

    glyph = ctx.theme.dinkus_medium or "✦"
    for index, paragraph in enumerate(paragraphs[1:], start=1):
        parts.append(f"<p>{escape(paragraph)}</p>")
        # Insert dinkus rest every 3rd paragraph (after paragraphs 3, 6, 9 …),
        # but not as the very last element.
        if index % 3 == 0 and index < len(paragraphs) - 1:
            parts.append(f'<hr class="ref-dinkus" data-glyph="{escape(glyph)}">')

    parts.append("</div>")
    return "".join(parts)


def present_lore_cosmology(node: object, ctx: PresenterContext) -> str:
    """Pull-quote block flanked by dinkus dividers."""
    text = str(node).strip()
    if not text:
        return ""
    glyph = ctx.theme.dinkus_medium or "✦"
    return (
        f'<hr class="ref-dinkus" data-glyph="{escape(glyph)}">'
        f'<p class="ref-pull-quote narrative-flourish">{escape(text)}</p>'
        f'<hr class="ref-dinkus" data-glyph="{escape(glyph)}">'
    )


# Register the prose presenters.
PRESENTERS.update(
    {
        ("lore", ("world_name",)): present_world_name_suppress,
        ("lore", ("setting_anchor",)): present_lore_setting_anchor,
        ("lore", ("history",)): present_lore_history,
        ("lore", ("cosmology",)): present_lore_cosmology,
    }
)


_DISPOSITION_KNOWN = frozenset({"friendly", "neutral", "wary", "hostile"})


def _disposition_badge(value: str) -> str:
    """Render a disposition pill. Unknown dispositions fall back to neutral
    class so we never emit an undefined CSS class (which would trip the
    chrome-wiring regression guard once it covers .ref-* classes)."""
    normalized = value.strip().lower()
    css_class = normalized if normalized in _DISPOSITION_KNOWN else "neutral"
    display = value.strip().title()
    return f'<span class="ref-badge ref-badge--disposition-{css_class}">{escape(display)}</span>'


def present_lore_factions(node: object, ctx: PresenterContext) -> str:
    if not isinstance(node, list) or not node:
        return ""
    cards: list[str] = []
    for item in node:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip() or "Unnamed"
        summary = str(item.get("summary", "")).strip()
        description = str(item.get("description", "")).strip()
        disposition = str(item.get("disposition", "neutral")).strip()
        slug = slugify(name)
        cards.append(
            f'<article class="ref-card" id="cult-{slug}">'
            '<div class="ref-card__kicker">Faction</div>'
            f'<h3 class="ref-card__title">{escape(name, quote=False)}</h3>'
            + (
                f'<div class="ref-card__summary">{escape(summary, quote=False)}</div>'
                if summary
                else ""
            )
            + (
                f'<p class="ref-card__body">{escape(description, quote=False)}</p>'
                if description
                else ""
            )
            + f'<div class="ref-card__meta">{_disposition_badge(disposition)}</div>'
            "</article>"
        )
    return (
        '<section class="ref-factions">'
        '<div class="ref-card-grid ref-card-grid--cols-3">' + "".join(cards) + "</div></section>"
    )


PRESENTERS[("lore", ("factions",))] = present_lore_factions
# Pack-tier factions.yaml reuses the same renderer — registry entry is
# present but inactive until a future task wires top-level-list dispatch.
PRESENTERS[("factions", ())] = present_lore_factions


def _format_chip_label(value: str) -> str:
    """snake_case → Title Case With Spaces, for chip labels."""
    return " ".join(part.capitalize() for part in str(value).replace("_", " ").split())


def _poi_image_html(*, slug: str, name: str, ctx: PresenterContext) -> str:
    """Story 63-8: an R2 landscape ``<img>`` for a location card, or "".

    Emits the image iff the location ``slug`` is in ``ctx.poi_image_slugs``
    (the history.yaml POI manifest). Fires an OTEL span on both outcomes so
    the decision is observable. Border/shadow tint uses the per-pack theme
    accent. Returns "" (text-only card) when there is no matching image — a
    spanned, observable skip, not a silent fallback."""
    if ctx.world is None:
        # No world context → no POI image possible. Observable, not silent.
        with reference_poi_image_not_found_span(pack=ctx.pack, world=None, slug=slug):
            pass
        return ""
    if slug in ctx.poi_image_slugs:
        src = resolve_asset_url(f"genre_packs/{ctx.pack}/worlds/{ctx.world}/assets/poi/{slug}.png")
        with reference_poi_image_resolved_span(pack=ctx.pack, world=ctx.world, slug=slug):
            pass
        # Escape the accent: it lands in a style= attribute and, while theme.yaml is
        # first-party today, the renderer's invariant is to escape every interpolation
        # (and the creator-authoring roadmap makes pack content less-trusted).
        accent = escape(ctx.theme.palette_accent)
        return (
            f'<img class="ref-card__poi" src="{escape(src)}" alt="{escape(name)}" '
            f'loading="lazy" style="width:100%;border:2px solid {accent};'
            f'box-shadow:0 2px 8px {accent}33;" />'
        )
    with reference_poi_image_not_found_span(pack=ctx.pack, world=ctx.world, slug=slug):
        pass
    return ""


def present_lore_geography(node: object, ctx: PresenterContext) -> str:
    # Accept both a top-level list and a dict with a single list-valued key
    # (e.g. {locations: [...]}).
    if isinstance(node, dict):
        for v in node.values():
            if isinstance(v, list):
                node = v
                break
    if not isinstance(node, list) or not node:
        return ""
    cards: list[str] = []
    for item in node:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip() or "Unnamed"
        item_id = str(item.get("id", slugify(name))).strip() or slugify(name)
        slug = slugify(item_id)
        region = str(item.get("region", "")).strip()
        type_ = str(item.get("type", "")).strip()
        environment = str(item.get("environment", "")).strip()
        description = str(item.get("description", "")).strip()
        chips: list[str] = []
        if type_:
            chips.append(f'<span class="ref-chip">{escape(_format_chip_label(type_))}</span>')
        if region:
            chips.append(f'<span class="ref-chip">{escape(_format_chip_label(region))}</span>')
        img_html = _poi_image_html(slug=slug, name=name, ctx=ctx)
        cards.append(
            f'<article class="ref-card" id="location-{slug}">'
            '<div class="ref-card__kicker">Location</div>'
            f'<h3 class="ref-card__title">{escape(name)}</h3>'
            + img_html
            + (f'<div class="ref-card__meta">{"".join(chips)}</div>' if chips else "")
            + (f'<div class="ref-card__summary">{escape(environment)}</div>' if environment else "")
            + (f'<p class="ref-card__body">{escape(description)}</p>' if description else "")
            + "</article>"
        )
    return (
        '<section class="ref-geography">'
        '<div class="ref-card-grid">' + "".join(cards) + "</div></section>"
    )


PRESENTERS[("lore", ("geography",))] = present_lore_geography
# Pack/world-tier locations.yaml — activated via file-root dispatch (Task 10).
PRESENTERS[("locations", ())] = present_lore_geography


def present_world_meta(node: object, ctx: PresenterContext) -> str:
    """Render world.yaml as a label-grid of key axes + starting conditions."""
    if not isinstance(node, dict):
        return ""
    description = str(node.get("description", "")).strip()
    axis_snapshot = node.get("axis_snapshot") or {}
    starting_location = str(node.get("starting_location", "")).strip()
    starting_time = str(node.get("starting_time", "")).strip()
    # cover_poi is a daemon hint — skip entirely.

    cells: list[str] = []
    axis_labels = {"scale": "Scale", "tone": "Tone", "swagger": "Swagger"}
    for key, label in axis_labels.items():
        value = str(axis_snapshot.get(key, "")).strip() if isinstance(axis_snapshot, dict) else ""
        if value:
            cells.append(
                f'<div class="ref-label-grid__cell">'
                f'<div class="ref-card__kicker">{escape(label)}</div>'
                f"<div>{escape(value)}</div>"
                f"</div>"
            )
    if starting_location:
        cells.append(
            f'<div class="ref-label-grid__cell">'
            f'<div class="ref-card__kicker">Starting Location</div>'
            f"<div>{escape(starting_location)}</div>"
            f"</div>"
        )
    if starting_time:
        cells.append(
            f'<div class="ref-label-grid__cell">'
            f'<div class="ref-card__kicker">Starting Time</div>'
            f"<div>{escape(starting_time)}</div>"
            f"</div>"
        )
    grid = f'<div class="ref-label-grid">{"".join(cells)}</div>'
    desc_html = f'<p class="narrative-flourish">{escape(description)}</p>' if description else ""
    return f'<section class="ref-world-meta">{desc_html}{grid}</section>'


PRESENTERS[("world", ())] = present_world_meta


def present_history_chapters(node: object, ctx: PresenterContext) -> str:
    """Render history.yaml chapters list as a vertical timeline."""
    if not isinstance(node, list) or not node:
        return ""
    chapters: list[str] = []
    for item in node:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip() or "Chapter"
        description = str(item.get("description", "")).strip()
        session_range = item.get("session_range")
        chip_html = ""
        if isinstance(session_range, list) and len(session_range) >= 2:
            chip_html = (
                f'<span class="ref-chip">Sessions {escape(str(session_range[0]))}'
                f"–{escape(str(session_range[1]))}</span>"
            )
        desc_html = f"<p>{escape(description)}</p>" if description else ""
        chapters.append(
            f'<section class="ref-timeline__chapter">'
            f'<h3 class="ref-card__title">{escape(label)}</h3>'
            f"{chip_html}"
            f"{desc_html}"
            f"</section>"
        )
    return f'<div class="ref-timeline">{"".join(chapters)}</div>'


PRESENTERS[("history", ("chapters",))] = present_history_chapters


def present_calendar(node: object, ctx: PresenterContext) -> str:
    """Render calendar.yaml as a key-value table."""
    if not isinstance(node, dict) or not node:
        return ""
    rows: list[str] = []
    for key, value in node.items():
        if isinstance(value, list):
            cell = escape(", ".join(str(v) for v in value))
        elif isinstance(value, dict):
            dumped = yaml.safe_dump(value, sort_keys=False, default_flow_style=True).strip()
            cell = f"<pre>{escape(dumped)}</pre>"
        else:
            cell = escape(str(value))
        rows.append(f"<tr><th>{escape(str(key))}</th><td>{cell}</td></tr>")
    return f'<table class="ref-table"><tbody>{"".join(rows)}</tbody></table>'


PRESENTERS[("calendar", ())] = present_calendar


def present_demographics(node: object, ctx: PresenterContext) -> str:
    """Render demographics.yaml as a label-grid of scalar key-value pairs."""
    if not isinstance(node, dict) or not node:
        return ""
    cells: list[str] = []
    for key, value in node.items():
        label = _format_chip_label(str(key))
        if isinstance(value, list):
            display = escape(", ".join(str(v) for v in value))
        elif isinstance(value, dict):
            # Nested dict: dump first sentence or YAML
            dumped = yaml.safe_dump(value, sort_keys=False, default_flow_style=True).strip()
            display = f"<pre>{escape(dumped)}</pre>"
        else:
            display = escape(str(value))
        cells.append(
            f'<div class="ref-label-grid__cell">'
            f'<div class="ref-card__kicker">{escape(label)}</div>'
            f"<div>{display}</div>"
            f"</div>"
        )
    return f'<div class="ref-label-grid">{"".join(cells)}</div>'


PRESENTERS[("demographics", ())] = present_demographics


def present_legends(node: object, ctx: PresenterContext) -> str:
    """Render legends.yaml as a vertical stack of long-form article cards."""
    # Accept both a top-level list and a dict with a single list-valued key.
    if isinstance(node, dict):
        for v in node.values():
            if isinstance(v, list):
                node = v
                break
    if not isinstance(node, list) or not node:
        return ""
    articles: list[str] = []
    for item in node:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip() or "Unnamed"
        era = str(item.get("era", "")).strip()
        summary = str(item.get("summary", "")).strip()
        cultural_impact = str(item.get("cultural_impact", "")).strip()
        slug = slugify(name)
        articles.append(
            f'<article class="ref-card" id="legend-{slug}">'
            f'<h3 class="ref-card__title">{escape(name)}</h3>'
            + (f'<div class="ref-card__kicker">{escape(era)}</div>' if era else "")
            + (f'<div class="ref-card__summary">{escape(summary)}</div>' if summary else "")
            + (
                f'<p class="ref-card__body">{escape(cultural_impact)}</p>'
                if cultural_impact
                else ""
            )
            + "</article>"
        )
    return f'<section class="ref-legends">{"".join(articles)}</section>'


PRESENTERS[("legends", ())] = present_legends


_OPENING_PROSE_FIELDS = ("establishing_narration", "prose", "hook")
_OPENING_TITLE_FIELDS = ("name", "title")


def present_openings(node: object, ctx: PresenterContext) -> str:
    """Render openings.yaml as a 3-column card grid with pull-quoted prose."""
    # Unwrap dict wrapper (e.g. {version:…, openings:[…]}).
    if isinstance(node, dict):
        for v in node.values():
            if isinstance(v, list):
                node = v
                break
    if not isinstance(node, list) or not node:
        return ""
    cards: list[str] = []
    for item in node:
        if not isinstance(item, dict):
            continue
        title = ""
        for f in _OPENING_TITLE_FIELDS:
            title = str(item.get(f, "")).strip()
            if title:
                break
        prose = ""
        for f in _OPENING_PROSE_FIELDS:
            prose = str(item.get(f, "")).strip()
            if prose:
                break
        cards.append(
            '<article class="ref-card">'
            + (f'<h3 class="ref-card__title">{escape(title)}</h3>' if title else "")
            + (f'<p class="ref-pull-quote">{escape(prose)}</p>' if prose else "")
            + "</article>"
        )
    return '<div class="ref-card-grid ref-card-grid--cols-3">' + "".join(cards) + "</div>"


PRESENTERS[("openings", ())] = present_openings


def present_cultures(node: object, ctx: PresenterContext) -> str:
    """Render cultures.yaml as a 3-column card grid. Skips `slots` (generator config)."""
    # Unwrap dict wrapper (e.g. {cultures: [...]}).
    if isinstance(node, dict):
        for v in node.values():
            if isinstance(v, list):
                node = v
                break
    if not isinstance(node, list) or not node:
        return ""
    cards: list[str] = []
    for item in node:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip() or "Unnamed"
        summary = str(item.get("summary", "")).strip()
        description = str(item.get("description", "")).strip()
        slug = slugify(name)
        cards.append(
            f'<article class="ref-card" id="culture-{slug}">'
            '<div class="ref-card__kicker">Culture</div>'
            f'<h3 class="ref-card__title">{escape(name)}</h3>'
            + (f'<div class="ref-card__summary">{escape(summary)}</div>' if summary else "")
            + (f'<p class="ref-card__body">{escape(description)}</p>' if description else "")
            + "</article>"
        )
    return '<div class="ref-card-grid ref-card-grid--cols-3">' + "".join(cards) + "</div>"


PRESENTERS[("cultures", ())] = present_cultures


def _axiom_card(label: str, value: object) -> str:
    return (
        '<div class="ref-stat-card">'
        f'<div class="ref-card__kicker">{escape(label)}</div>'
        f"<div>{escape(str(value))}</div>"
        "</div>"
    )


def _label_cell(label: str, value: object) -> str:
    return (
        '<div class="ref-label-grid__cell">'
        f'<div class="ref-card__kicker">{escape(label)}</div>'
        f"<div>{escape(str(value))}</div>"
        "</div>"
    )


def _chip_strip(title: str, items: list) -> str:
    if not items:
        return ""
    chips = "".join(f'<span class="ref-chip">{escape(str(name))}</span>' for name in items)
    return f'<section class="ref-allowed"><h3>{escape(title)}</h3><div>{chips}</div></section>'


_AXIOM_PRIORITY: tuple[tuple[str, str], ...] = (
    ("stat_generation", "Stat Generation"),
    ("magic_level", "Magic Level"),
    ("lethality", "Lethality"),
    ("tone", "Tone"),
    ("default_class", "Default Class"),
)


def present_rules_root(node: object, ctx: PresenterContext) -> str:
    """Top-level rules.yaml presenter — emits an axiom strip, default frame,
    allowed-class/race chip strips, opening location pull-quote, and a
    vertical custom_rules card list. Mechanical configs (confrontations,
    resources, edge_config, chargen_field_labels) are intentionally
    skipped — the rules page is high-altitude orientation, not a full
    system reference. Their KEEPER paths (narrator_hint descendants) are
    already covered by reference_visibility."""
    if not isinstance(node, dict):
        return ""
    parts: list[str] = []

    # 1. Axiom strip — only emit cards for keys present in node.
    cards = [
        _axiom_card(label, node[key])
        for key, label in _AXIOM_PRIORITY
        if key in node and node[key] not in (None, "")
    ]
    if cards:
        parts.append('<div class="ref-stat-strip">' + "".join(cards) + "</div>")

    # 2. Default frame.
    frame_cells: list[str] = []
    if node.get("default_class"):
        frame_cells.append(
            _label_cell(str(node.get("class_label") or "Class"), node["default_class"])
        )
    if node.get("default_race"):
        frame_cells.append(_label_cell(str(node.get("race_label") or "Race"), node["default_race"]))
    if node.get("default_time_of_day"):
        frame_cells.append(_label_cell("Time of Day", node["default_time_of_day"]))
    if node.get("point_buy_budget"):
        frame_cells.append(_label_cell("Point Buy", node["point_buy_budget"]))
    abil = node.get("ability_score_names")
    if isinstance(abil, list) and abil:
        frame_cells.append(_label_cell("Ability Scores", ", ".join(str(a) for a in abil)))
    if frame_cells:
        parts.append('<div class="ref-label-grid">' + "".join(frame_cells) + "</div>")

    # 3. Allowed-classes / allowed-races chip strips.
    parts.append(_chip_strip("Classes", node.get("allowed_classes") or []))
    parts.append(_chip_strip("Origins", node.get("allowed_races") or []))

    # 4. Opening location pull-quote.
    default_location = node.get("default_location")
    if default_location:
        parts.append(
            f'<p class="ref-pull-quote narrative-flourish">{escape(str(default_location))}</p>'
        )

    # 5. Custom rules cards.
    custom = node.get("custom_rules")
    if isinstance(custom, dict) and custom:
        cards_html: list[str] = []
        for key, prose in custom.items():
            if not prose:
                continue
            cards_html.append(
                '<div class="ref-card">'
                f'<div class="ref-card__kicker">{escape(_format_chip_label(str(key)))}</div>'
                f'<p class="ref-card__body">{escape(str(prose))}</p>'
                "</div>"
            )
        if cards_html:
            parts.append('<section class="ref-custom-rules">' + "".join(cards_html) + "</section>")

    return "".join(parts)


PRESENTERS[("rules", ())] = present_rules_root


def _picker_chip_strip(title: str, items: list) -> str:
    """Inline labeled chip row inside a panel."""
    if not isinstance(items, list) or not items:
        return ""
    chips = "".join(f'<span class="ref-chip">{escape(str(it))}</span>' for it in items)
    return (
        '<div class="ref-card__meta">'
        f'<div class="ref-card__kicker">{escape(title)}</div>'
        f"<div>{chips}</div>"
        "</div>"
    )


def _first_field(item: dict, fields: tuple[str, ...]) -> str:
    for f in fields:
        v = item.get(f)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _render_picker(
    items: list,
    item_kicker: str,
    hash_prefix: str,
    *,
    name_fields: tuple[str, ...],
    panel_body: Callable[[dict], str],
) -> str:
    """Shared picker shape — chip row + panel stack with data-island='picker'.
    First item is default-selected; others hidden until JS hydrates."""
    if not isinstance(items, list) or not items:
        return ""
    chips: list[str] = []
    panels: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = _first_field(item, name_fields)
        item_id = str(item.get("id") or name or f"item-{index}").strip()
        slug = slugify(item_id)
        if not name:
            name = item_id
        is_default = index == 0
        chips.append(
            f'<button type="button" class="ref-picker__chip" '
            f'data-target="{hash_prefix}-{slug}" '
            f'aria-selected="{"true" if is_default else "false"}">'
            f"{escape(name)}</button>"
        )
        body_html = panel_body(item)
        hidden_attr = "" if is_default else " hidden"
        panels.append(
            f'<section class="ref-picker-panel" '
            f'id="{hash_prefix}-{slug}" '
            f'data-panel="{hash_prefix}-{slug}"{hidden_attr}>'
            f'<div class="ref-card__kicker">{escape(item_kicker)}</div>'
            f'<h3 class="ref-card__title">{escape(name)}</h3>' + body_html + "</section>"
        )
    if not chips:
        return ""
    return (
        '<div data-island="picker">'
        f'<div class="ref-picker">{"".join(chips)}</div>' + "".join(panels) + "</div>"
    )


def _archetype_panel_body(item: dict) -> str:
    parts: list[str] = []
    desc = item.get("description")
    if desc:
        parts.append(f'<p class="ref-card__body">{escape(str(desc))}</p>')
    parts.append(_picker_chip_strip("Personality", item.get("personality_traits") or []))
    parts.append(_picker_chip_strip("Typical Classes", item.get("typical_classes") or []))
    parts.append(_picker_chip_strip("Typical Origins", item.get("typical_races") or []))
    parts.append(_picker_chip_strip("Dialogue Quirks", item.get("dialogue_quirks") or []))
    return "".join(parts)


def _class_panel_body(item: dict) -> str:
    parts: list[str] = []
    flavor = item.get("flavor")
    if flavor:
        parts.append(f'<p class="ref-card__body">{escape(str(flavor))}</p>')
    # Label grid for role + prime requisite + magic access (if any present and non-null).
    cells: list[str] = []
    for key, label in (
        ("rpg_role", "Role"),
        ("prime_requisite", "Prime Req"),
        ("magic_access", "Magic"),
    ):
        val = item.get(key)
        if val in (None, "", []):
            continue
        cells.append(_label_cell(label, val))
    if cells:
        parts.append('<div class="ref-label-grid">' + "".join(cells) + "</div>")
    parts.append(_picker_chip_strip("Beat Choices", item.get("encounter_beat_choices") or []))
    # Story 71-1: signature ability (ADR-095 — exactly one per non-magical
    # class). Render only when present and non-empty; magical classes carry no
    # abilities list and emit no block. The `involuntary` flag is not a render
    # filter — take the first ability as authored.
    abilities = item.get("abilities")
    if isinstance(abilities, list) and abilities:
        ability = abilities[0]
        if isinstance(ability, dict):
            name = str(ability.get("name", "")).strip()
            genre_description = str(ability.get("genre_description", "")).strip()
            mechanical_effect = str(ability.get("mechanical_effect", "")).strip()
            parts.append(
                '<div class="ref-card__ability">'
                '<div class="ref-card__kicker">Signature Ability</div>'
                + (
                    f'<div class="ref-card__ability-name">{escape(name)}</div>'
                    if name
                    else ""
                )
                + (
                    f'<p class="ref-card__body">{escape(genre_description)}</p>'
                    if genre_description
                    else ""
                )
                + (
                    f'<div class="ref-card__ability-effect">{escape(mechanical_effect)}</div>'
                    if mechanical_effect
                    else ""
                )
                + "</div>"
            )
    return "".join(parts)


def _unwrap_list(node: object) -> list | None:
    """Accept either a top-level list OR a dict whose first list-valued
    entry IS the list (the common YAML wrapper pattern). Return the list
    or None if no list is reachable."""
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        for value in node.values():
            if isinstance(value, list):
                return value
    return None


def present_archetypes_picker(node: object, ctx: PresenterContext) -> str:
    items = _unwrap_list(node)
    if items is None:
        return ""
    return _render_picker(
        items,
        "Archetype",
        "archetype",
        name_fields=("name",),
        panel_body=_archetype_panel_body,
    )


def present_classes_picker(node: object, ctx: PresenterContext) -> str:
    items = _unwrap_list(node)
    if items is None:
        return ""
    return _render_picker(
        items,
        "Class",
        "class",
        name_fields=("display_name", "name"),
        panel_body=_class_panel_body,
    )


PRESENTERS[("archetypes", ())] = present_archetypes_picker
PRESENTERS[("classes", ())] = present_classes_picker


def present_progression(node: object, ctx: PresenterContext) -> str:
    """Render progression.yaml affinities as a vertical card stack."""
    if not isinstance(node, dict):
        return ""
    affinities = node.get("affinities")
    if not isinstance(affinities, list) or not affinities:
        return ""
    cards: list[str] = []
    for item in affinities:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip() or "Unnamed"
        description = str(item.get("description", "")).strip()
        triggers = item.get("triggers") or []
        tier_thresholds = item.get("tier_thresholds")
        inner: list[str] = [
            '<div class="ref-card__kicker">Affinity</div>',
            f'<h3 class="ref-card__title">{escape(name)}</h3>',
        ]
        if description:
            inner.append(f'<p class="ref-card__summary">{escape(description)}</p>')
        if isinstance(triggers, list) and triggers:
            chips = "".join(f'<span class="ref-chip">{escape(str(t))}</span>' for t in triggers)
            inner.append(f'<div class="ref-card__meta">{chips}</div>')
        if isinstance(tier_thresholds, list) and len(tier_thresholds) >= 3:
            label = " / ".join(str(t) for t in tier_thresholds[:3])
            inner.append(
                f'<div class="ref-card__meta">'
                f'<span class="ref-chip">Tier thresholds: {escape(label)}</span>'
                f"</div>"
            )
        cards.append('<article class="ref-card">' + "".join(inner) + "</article>")
    return '<section class="ref-progression">' + "".join(cards) + "</section>"


PRESENTERS[("progression", ())] = present_progression


def _magic_scalar_default(node: dict, key: str) -> str | None:
    """Read a magic field's player-facing scalar, tolerating both magic.yaml
    shapes: a nested ``{key: {default: <v>, permitted: [...]}}`` block OR a
    flat ``{key_default: <v>}`` / ``{key: <scalar>}``. Returns only the
    *default* (the evocative one-word state) — the ``permitted`` enum is
    tuning noise and is suppressed.
    """
    block = node.get(key)
    if isinstance(block, dict):
        val = block.get("default")
    elif isinstance(block, str):
        val = block
    else:
        val = node.get(f"{key}_default")
    val = str(val).strip() if val is not None else ""
    return val or None


def present_magic(node: object, ctx: PresenterContext) -> str:
    """Render magic.yaml as player-facing prose.

    magic.yaml ships in two shapes across packs: a flat root dict (caverns,
    space_opera) and a dict wrapped under a top-level ``magic:`` key
    (mutant_wasteland, road_warrior, spaghetti_western, tea_and_murder,
    heavy_metal worlds). This presenter unwraps the wrapper and renders the
    player-meaningful fields (sources of power, costs, hard limits, counters,
    manifestation) as cards/chips. Dev-tuning and DM-voice keys — intensity
    numbers, permitted-enum ranges, reliability internals, player_options
    flags, plugin lists, and ``narrator_register`` (a DM instruction) — are
    deliberately suppressed; they are not player-facing reference content.
    Returning non-empty for any populated magic.yaml prevents the generic
    renderer from falling through to a raw config dump (playtest 2026-05-25).
    """
    if not isinstance(node, dict):
        return ""
    inner = node.get("magic")
    magic = inner if isinstance(inner, dict) else node
    if not isinstance(magic, dict):
        return ""

    parts: list[str] = []

    # Label grid: genre + single-word world state (default only, no enum).
    cells: list[str] = []
    genre = str(magic.get("genre", "")).strip()
    if genre:
        cells.append(_label_cell("Genre", genre))
    knowledge = _magic_scalar_default(magic, "world_knowledge")
    if knowledge:
        cells.append(_label_cell("Common Knowledge", _format_chip_label(knowledge)))
    attitude = _magic_scalar_default(magic, "visibility")
    if attitude:
        cells.append(_label_cell("Attitude", _format_chip_label(attitude)))
    if cells:
        parts.append('<div class="ref-label-grid">' + "".join(cells) + "</div>")

    # Sources of power: list[str] (flat) or list[dict] (wrapped, with
    # label + examples). DM-voice ``narrator_note`` is intentionally dropped.
    sources = magic.get("allowed_sources")
    if isinstance(sources, list) and sources:
        if all(isinstance(s, str) for s in sources):
            strip = _chip_strip("Sources of Power", [_format_chip_label(str(s)) for s in sources])
            if strip:
                parts.append(strip)
        else:
            cards: list[str] = []
            for src in sources:
                if isinstance(src, str):
                    label, examples = _format_chip_label(src), []
                elif isinstance(src, dict):
                    label = str(
                        src.get("label") or _format_chip_label(str(src.get("id", "")))
                    ).strip()
                    examples = src.get("examples") if isinstance(src.get("examples"), list) else []
                else:
                    continue
                if not label:
                    continue
                body = ""
                if examples:
                    chips = "".join(
                        f'<span class="ref-chip">{escape(str(e))}</span>' for e in examples
                    )
                    body = f"<div>{chips}</div>"
                cards.append(
                    '<article class="ref-card">'
                    '<div class="ref-card__kicker">Source</div>'
                    f"<h3>{escape(label)}</h3>{body}</article>"
                )
            if cards:
                parts.append(
                    '<section class="ref-allowed"><h3>Sources of Power</h3>'
                    '<div class="ref-card-grid">' + "".join(cards) + "</div></section>"
                )

    # Costs: required_costs (wrapped) or cost_types (flat).
    costs = magic.get("required_costs")
    if not (isinstance(costs, list) and costs):
        costs = magic.get("cost_types")
    if isinstance(costs, list) and costs:
        strip = _chip_strip("Every Working Costs", [_format_chip_label(str(c)) for c in costs])
        if strip:
            parts.append(strip)

    # Hard limits: dict {name: verdict} or list[str].
    limits = magic.get("hard_limits")
    if isinstance(limits, dict) and limits:
        rows = "".join(
            f"<li><strong>{escape(_format_chip_label(str(k)))}</strong>: "
            f"{escape(_format_chip_label(str(v)))}</li>"
            for k, v in limits.items()
        )
        parts.append(f'<section class="ref-allowed"><h3>Hard Limits</h3><ul>{rows}</ul></section>')
    elif isinstance(limits, list) and limits:
        strip = _chip_strip("Hard Limits", [_format_chip_label(str(x)) for x in limits])
        if strip:
            parts.append(strip)

    # Counters: list[dict {id, description}] or list[str].
    counters = magic.get("counter")
    if isinstance(counters, list) and counters:
        rows: list[str] = []
        for c in counters:
            if isinstance(c, dict):
                label = _format_chip_label(str(c.get("id", "")))
                desc = str(c.get("description", "")).strip()
                if not label:
                    continue
                rows.append(
                    f"<li><strong>{escape(label)}</strong>"
                    f"{(' — ' + escape(desc)) if desc else ''}</li>"
                )
            elif isinstance(c, str):
                rows.append(f"<li>{escape(_format_chip_label(c))}</li>")
        if rows:
            parts.append(
                '<section class="ref-allowed"><h3>Counters</h3><ul>'
                + "".join(rows)
                + "</ul></section>"
            )

    # Manifestation: {modes, domains}.
    manifestation = magic.get("manifestation")
    if isinstance(manifestation, dict):
        modes = manifestation.get("modes")
        if isinstance(modes, list) and modes:
            strip = _chip_strip("Manifests As", [_format_chip_label(str(m)) for m in modes])
            if strip:
                parts.append(strip)
        domains = manifestation.get("domains")
        if isinstance(domains, list) and domains:
            strip = _chip_strip("Domains", [_format_chip_label(str(d)) for d in domains])
            if strip:
                parts.append(strip)

    return "".join(parts)


PRESENTERS[("magic", ())] = present_magic


def present_power_tiers(node: object, ctx: PresenterContext) -> str:
    """Render power_tiers.yaml — one table per class. npc column is never emitted."""
    if not isinstance(node, dict) or not node:
        return ""
    sections: list[str] = []
    for class_name, tiers in node.items():
        if not isinstance(tiers, list) or not tiers:
            continue
        rows: list[str] = []
        for item in tiers:
            if not isinstance(item, dict):
                continue
            level_range = item.get("level_range")
            if isinstance(level_range, list) and len(level_range) >= 2:
                level_cell = f"{level_range[0]}–{level_range[1]}"
            else:
                level_cell = str(level_range) if level_range is not None else ""
            label = str(item.get("label", "")).strip()
            player = str(item.get("player", "")).strip()
            rows.append(
                f"<tr>"
                f"<td>{escape(level_cell)}</td>"
                f"<td>{escape(label)}</td>"
                f"<td>{escape(player)}</td>"
                f"</tr>"
            )
        if not rows:
            continue
        thead = "<thead><tr><th>Level</th><th>Label</th><th>Player View</th></tr></thead>"
        sections.append(
            "<section>"
            f"<h3>{escape(str(class_name))}</h3>"
            f'<table class="ref-table">{thead}<tbody>{"".join(rows)}</tbody></table>'
            "</section>"
        )
    if not sections:
        return ""
    return '<section class="ref-power-tiers">' + "".join(sections) + "</section>"


PRESENTERS[("power_tiers", ())] = present_power_tiers


def present_achievements(node: object, ctx: PresenterContext) -> str:
    """Render achievements.yaml as a 3-column card grid."""
    items = _unwrap_list(node)
    if not items:
        return ""
    cards: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip() or "Unnamed"
        condition = str(item.get("condition", "")).strip()
        reward = str(item.get("reward", "")).strip()
        inner: list[str] = [
            '<div class="ref-card__kicker">Achievement</div>',
            f'<h3 class="ref-card__title">{escape(name)}</h3>',
        ]
        if condition:
            inner.append(f'<p class="ref-card__body">{escape(condition)}</p>')
        if reward:
            inner.append(
                f'<div class="ref-card__meta"><span class="ref-chip">{escape(reward)}</span></div>'
            )
        cards.append('<article class="ref-card">' + "".join(inner) + "</article>")
    if not cards:
        return ""
    return '<div class="ref-card-grid ref-card-grid--cols-3">' + "".join(cards) + "</div>"


PRESENTERS[("achievements", ())] = present_achievements


def present_inventory(node: object, ctx: PresenterContext) -> str:
    """Render inventory.yaml — currency label-grid + item catalog table."""
    if not isinstance(node, dict):
        return ""
    parts: list[str] = []

    currency = node.get("currency")
    if isinstance(currency, dict) and currency:
        name = str(currency.get("name", "")).strip()
        denoms = currency.get("denominations") or []
        cells: list[str] = []
        if name:
            cells.append(_label_cell("Currency", name))
        if isinstance(denoms, list) and denoms:
            chips = ", ".join(str(d) for d in denoms)
            cells.append(
                '<div class="ref-label-grid__cell">'
                '<div class="ref-card__kicker">Denominations</div>'
                f"<div>{escape(chips)}</div>"
                "</div>"
            )
        if cells:
            parts.append('<div class="ref-label-grid">' + "".join(cells) + "</div>")

    catalog = node.get("item_catalog")
    if isinstance(catalog, list) and catalog:
        rows: list[str] = []
        for item in catalog:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            category = str(item.get("category", "")).strip()
            value = str(item.get("value", "")).strip()
            weight = str(item.get("weight", "")).strip()
            rarity = str(item.get("rarity", "")).strip()
            description = str(item.get("description", "")).strip()
            rows.append(
                f"<tr>"
                f"<td>{escape(name)}</td>"
                f"<td>{escape(category)}</td>"
                f"<td>{escape(value)}</td>"
                f"<td>{escape(weight)}</td>"
                f"<td>{escape(rarity)}</td>"
                f"<td>{escape(description)}</td>"
                f"</tr>"
            )
        if rows:
            thead = (
                "<thead><tr>"
                "<th>Name</th><th>Category</th><th>Value</th>"
                "<th>Weight</th><th>Rarity</th><th>Description</th>"
                "</tr></thead>"
            )
            parts.append(f'<table class="ref-table">{thead}<tbody>{"".join(rows)}</tbody></table>')

    if not parts:
        return ""
    return "".join(parts)


PRESENTERS[("inventory", ())] = present_inventory


def present_equipment_tables(node: object, ctx: PresenterContext) -> str:
    """Render equipment_tables.yaml — one h3+table per list-valued top-level key."""
    if not isinstance(node, dict) or not node:
        return ""
    parts: list[str] = []
    for key, value in node.items():
        if not isinstance(value, list) or not value:
            continue
        rows: list[str] = []
        headers: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if not headers:
                    headers = list(item.keys())
                rows.append(
                    "<tr>"
                    + "".join(f"<td>{escape(str(item.get(h, '')))}</td>" for h in headers)
                    + "</tr>"
                )
            else:
                if not headers:
                    headers = ["Value"]
                rows.append(f"<tr><td>{escape(str(item))}</td></tr>")
        if not rows:
            continue
        thead = (
            "<thead><tr>"
            + "".join(f"<th>{escape(_format_chip_label(h))}</th>" for h in headers)
            + "</tr></thead>"
        )
        parts.append(
            f"<h3>{escape(_format_chip_label(str(key)))}</h3>"
            f'<table class="ref-table">{thead}<tbody>{"".join(rows)}</tbody></table>'
        )
    if not parts:
        return ""
    return "".join(parts)


PRESENTERS[("equipment_tables", ())] = present_equipment_tables


def present_beat_vocabulary(node: object, ctx: PresenterContext) -> str:
    """Render beat_vocabulary.yaml as dl sections per list-of-dict key.

    Skips the `obstacles` key explicitly — KEEPER content for the narrator only.
    """
    if not isinstance(node, dict) or not node:
        return ""
    parts: list[str] = []
    for key, value in node.items():
        if key == "obstacles":
            continue
        if not isinstance(value, list) or not value:
            continue
        # Only render if items are dicts with name/description fields.
        terms: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            description = str(item.get("description", "")).strip()
            if name or description:
                terms.append(
                    f"<dt>{escape(name)}</dt>"
                    + (f"<dd>{escape(description)}</dd>" if description else "")
                )
        if not terms:
            continue
        parts.append(
            f"<section>"
            f"<h3>{escape(_format_chip_label(str(key)))}</h3>"
            f"<dl>{''.join(terms)}</dl>"
            f"</section>"
        )
    if not parts:
        return ""
    return "".join(parts)


PRESENTERS[("beat_vocabulary", ())] = present_beat_vocabulary
