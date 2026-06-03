"""Story 65-12 — lore-page world Timeline section.

The lore reference page (``GET /reference/lore/{pack}/{world}``, ADR-135 public
projection) gains a **Timeline** section: a world-historical spine built from the
world's legends. Per the 65-12 design the spine is **legends only** — campaign
``history.yaml:chapters`` ride a different (play-time) axis and carry dormant-trope
spoiler seeds (ADR-135 D1), and POI founding has no authored field (deferred to
74-3).

The one genuinely new behaviour is an **honest conditional sort**. The temporal
value authors write (``era``, falling back to ``period``) is free-text and
bespoke per world — absolute years, relative phrases, fantasy calendars, prose.
So the spine sorts dated entries ascending ONLY when every one exposes a
uniformly-parseable key (a signed integer year); otherwise it preserves authored
order. The render records which mode fired in an OTEL span so the page never
claims a chronology it could not compute. Undated legends (no era and no period)
always follow the dated spine, in authored order.

This module owns only the new code (the legend/history loaders, the conditional
sort, and the HTML emission). The legend parser (``_load_legends_flexible``), the
slug rule (``slugify_player_name``), and the section/TOC append in
``assemble_lore_page`` are reused, not rebuilt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

import yaml

from sidequest.genre.loader import _load_legends_flexible
from sidequest.genre.models.legends import Legend
from sidequest.server.utils import slugify_player_name
from sidequest.telemetry.spans.reference import reference_timeline_rendered_span

# A temporal value is "uniformly parseable" only if it is a bare signed integer
# year (e.g. "1612", "-11540"). Relative phrases ("15 years ago"), named ages
# ("early Second Rising"), and prose dates are intentionally NOT parsed: the
# spine falls back to authored order rather than fabricating a cross-dialect
# chronology (see Design Deviation — relative-family sorting is not implemented).
_YEAR_RE = re.compile(r"^-?\d+$")


@dataclass(frozen=True)
class _Entry:
    """One legend projected onto the timeline."""

    slug: str
    name: str
    summary: str
    temporal: str | None  # verbatim era-else-period; None == undated


def load_legends(world_dir: Path) -> list[Legend]:
    """Load ``world_dir/legends.yaml`` into typed ``Legend`` records.

    Returns ``[]`` when the world authors no legends (the Timeline section is
    purely additive — a world without legends renders unchanged). Fails **loud**
    on a malformed file (No Silent Fallbacks): ``_load_legends_flexible`` raises
    ``GenreLoadError`` for a shape the ``Legend`` model rejects, which the lore
    route surfaces as HTTP 500 rather than a silently timeline-less page.
    """
    legends, _raw = _load_legends_flexible(world_dir / "legends.yaml")
    return legends


def load_lore_history(world_dir: Path) -> str | None:
    """Return the ``history`` prose from ``world_dir/lore.yaml``, or ``None``.

    Used only as the Timeline section's framing preamble — never decomposed into
    entries. Absent file, absent field, or non-string value all yield ``None``
    (no preamble renders).
    """
    path = world_dir / "lore.yaml"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if isinstance(data, dict):
        history = data.get("history")
        if isinstance(history, str) and history.strip():
            return history
    return None


def _temporal_of(legend: Legend) -> str | None:
    """The verbatim temporal value: ``era`` if non-empty, else ``period``."""
    era = (legend.era or "").strip()
    if era:
        return era
    period = (legend.period or "").strip()
    return period or None


def _year_key(temporal: str) -> int | None:
    """Parse a bare signed-integer year, or ``None`` if not a clean year."""
    if _YEAR_RE.match(temporal.strip()):
        return int(temporal.strip())
    return None


def present_lore_timeline(legends: list[Legend], *, history_prose: str | None) -> str:
    """Render the Timeline ``<section>`` (heading + optional preamble + entries),
    or "" if there are no legends.

    Emits the ``timeline_rendered`` OTEL span recording the entry census and the
    sort mode the GM/dev panel reads.
    """
    if not legends:
        return ""

    entries = [
        _Entry(
            slug=slugify_player_name(lg.name),
            name=lg.name,
            summary=lg.summary,
            temporal=_temporal_of(lg),
        )
        for lg in legends
    ]
    dated = [e for e in entries if e.temporal is not None]
    undated = [e for e in entries if e.temporal is None]

    # Honest conditional sort: only when EVERY dated entry is a clean year.
    if dated and all(_year_key(e.temporal or "") is not None for e in dated):
        sort_mode = "sorted"
        # Stable ascending sort; `or 0` is unreachable (all keys parse here) and
        # only narrows the type for the checker.
        ordered_dated = sorted(dated, key=lambda e: _year_key(e.temporal or "") or 0)
    else:
        sort_mode = "authored_order"
        ordered_dated = list(dated)

    parts: list[str] = ['<section id="timeline" class="ref-timeline">', "<h2>Timeline</h2>"]
    if history_prose:
        parts.append(f'<div class="ref-timeline__preamble"><p>{escape(history_prose)}</p></div>')

    parts.append('<ol class="ref-timeline__list">')
    for e in ordered_dated:
        temporal = e.temporal or ""
        parts.append(
            f'<li class="ref-timeline__entry" data-timeline-entry="{escape(e.slug)}" '
            f'data-temporal="{escape(temporal)}">'
            f'<span class="ref-timeline__era">{escape(temporal)}</span>'
            f'<span class="ref-timeline__name">{escape(e.name)}</span>'
            f'<p class="ref-timeline__summary">{escape(e.summary)}</p>'
            f"</li>"
        )
    parts.append("</ol>")

    if undated:
        parts.append('<div class="ref-timeline__undated" data-timeline-group="undated">')
        parts.append("<h3>Undated</h3><ul>")
        for e in undated:
            parts.append(
                f'<li class="ref-timeline__entry" data-timeline-entry="{escape(e.slug)}">'
                f'<span class="ref-timeline__name">{escape(e.name)}</span>'
                f'<p class="ref-timeline__summary">{escape(e.summary)}</p>'
                f"</li>"
            )
        parts.append("</ul></div>")

    parts.append("</section>")

    with reference_timeline_rendered_span(
        entry_count=len(entries),
        undated_count=len(undated),
        sort_mode=sort_mode,
    ):
        pass

    return "".join(parts)
