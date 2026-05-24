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
