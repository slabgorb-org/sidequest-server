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

from sidequest.server.reference_slug import slugify
from sidequest.server.reference_theme import ReferenceTheme

KeyPath = tuple[str, ...]


@dataclass(frozen=True)
class PresenterContext:
    pack: str
    world: str | None
    file_stem: str
    key_path: KeyPath
    theme: ReferenceTheme
    depth: int


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
        cards.append(
            f'<article class="ref-card" id="location-{slug}">'
            '<div class="ref-card__kicker">Location</div>'
            f'<h3 class="ref-card__title">{escape(name)}</h3>'
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
