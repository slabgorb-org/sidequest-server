"""chart.* OTEL spans — orbital chart rendering.

Per spec §7.3 — Keith's GM panel sees scope_center, t_hours, party_at,
body_count, output_size_bytes for every chart render. Validates that
re-renders happen on the expected schedule (clock advance, drill
in/out) and that the renderer isn't quietly producing empty SVGs.

Story 45-42 (orrery v2) extends the span with register-population counts
and label-density signals so the GM panel can verify the new register
pipeline fired:
  - body_count_engraved / chalk / prose: how many bodies in each register.
  - body_count_moons_rendered: how many moons rendered in the
    system-scope moon band (vs. elided via show_at_system_scope=False).
  - label_collision_tier_max: highest peer-collision tier the label
    placement pass assigned (0 means no clusters needed offsetting).
"""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS
from .span import Span

SPAN_CHART_RENDER = "chart.render"
SPAN_CHART_LABEL_STRATEGY = "chart.label_strategy"
SPAN_CHART_LABEL_DISTRIBUTION = "chart.label_distribution"

FLAT_ONLY_SPANS.update(
    {
        SPAN_CHART_RENDER,
        SPAN_CHART_LABEL_STRATEGY,
        SPAN_CHART_LABEL_DISTRIBUTION,
    }
)


def emit_chart_render(
    *,
    scope_center: str,
    t_hours: float,
    party_at: str | None,
    body_count: int,
    output_size_bytes: int,
    body_count_engraved: int = 0,
    body_count_chalk: int = 0,
    body_count_prose: int = 0,
    body_count_moons_rendered: int = 0,
    label_collision_tier_max: int = 0,
) -> None:
    """Emit a `chart.render` span. Fire-and-forget (FLAT_ONLY_SPANS).

    The new register/moon/label-density attributes default to 0 so existing
    callers don't break, but `render_chart` always passes the real counts so
    the GM panel can audit the orrery-v2 register pipeline.
    """
    with Span.open(
        SPAN_CHART_RENDER,
        attrs={
            "scope_center": scope_center,
            "t_hours": float(t_hours),
            "party_at": party_at if party_at is not None else "",
            "body_count": int(body_count),
            "output_size_bytes": int(output_size_bytes),
            "body_count_engraved": int(body_count_engraved),
            "body_count_chalk": int(body_count_chalk),
            "body_count_prose": int(body_count_prose),
            "body_count_moons_rendered": int(body_count_moons_rendered),
            "label_collision_tier_max": int(label_collision_tier_max),
        },
    ):
        pass


def emit_chart_label_strategy(
    *,
    body_id: str,
    parent_id: str | None,
    parent_type: str | None,
    strategy_chosen: str,
    selection_reason: str,
    tier: int | None,
    arc_available_px: float | None,
    text_width_px: float,
    path_circumference_px: float | None,
) -> None:
    """Emit one chart.label_strategy span per labeled body per render (ADR-094).

    Strategy-specific fields use sentinel values when not applicable:
      tier = -1 when not radial
      arc_available_px = -1.0 when not radial
      path_circumference_px = -1.0 when not textpath
    OTEL discourages None in attrs; sentinels keep the schema stable.
    """
    with Span.open(
        SPAN_CHART_LABEL_STRATEGY,
        attrs={
            "body_id": body_id,
            "parent_id": parent_id or "",
            "parent_type": parent_type or "",
            "strategy_chosen": strategy_chosen,
            "selection_reason": selection_reason,
            "tier": -1 if tier is None else int(tier),
            "arc_available_px": -1.0 if arc_available_px is None else float(arc_available_px),
            "text_width_px": float(text_width_px),
            "path_circumference_px": -1.0
            if path_circumference_px is None
            else float(path_circumference_px),
        },
    ):
        pass


def emit_chart_label_distribution(
    *,
    bodies_total: int,
    bodies_textpath: int,
    bodies_radial: int,
    bodies_callout: int,
    bodies_unlabeled: int,
    gutter_inset_fallbacks: int,
    cross_group_crossings: int,
) -> None:
    """Emit one chart.label_distribution span per render — aggregates strategy counts (ADR-094 AC-O2)."""
    with Span.open(
        SPAN_CHART_LABEL_DISTRIBUTION,
        attrs={
            "bodies_total": int(bodies_total),
            "bodies_textpath": int(bodies_textpath),
            "bodies_radial": int(bodies_radial),
            "bodies_callout": int(bodies_callout),
            "bodies_unlabeled": int(bodies_unlabeled),
            "gutter_inset_fallbacks": int(gutter_inset_fallbacks),
            "cross_group_crossings": int(cross_group_crossings),
        },
    ):
        pass
