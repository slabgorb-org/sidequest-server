"""Movement subsystem spans (Movement Subsystem §OTEL Contract).

The GM panel is the lie detector: movement must be provably engaged
vs. improvised. ``movement.resolved`` (INFO) fires once per PC who
actually moved through a real graph adjacency; ``movement.unresolved``
(ERROR) fires once per PC whose coarse intent could not be resolved to
a real adjacency (fail-loud, never a silent stay-put or invented
region).

Both spans are PER-PC — every span carries ``pc_name`` so the panel
sees each PC's move independently (split-party legibility). They match
the ``frontier_region_transition_span`` context-manager shape in
``dungeon_materialize.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# ---------------------------------------------------------------------------
# Span name constants
# ---------------------------------------------------------------------------

SPAN_MOVEMENT_RESOLVED = "movement.resolved"
SPAN_MOVEMENT_UNRESOLVED = "movement.unresolved"

# ---------------------------------------------------------------------------
# Routing registrations
# ---------------------------------------------------------------------------


def _attr(field: str):
    return lambda span, f=field: (span.attributes or {}).get(f)


SPAN_ROUTES[SPAN_MOVEMENT_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="movement",
    extract=lambda s: {
        "field": "pc_regions",
        "op": "movement.resolved",
        "pc_name": _attr("pc_name")(s),
        "from_region": _attr("from_region")(s),
        "to_region": _attr("to_region")(s),
        "intent.direction": _attr("intent.direction")(s),
        "intent.exit_descriptor": _attr("intent.exit_descriptor")(s),
        "resolved_via": _attr("resolved_via")(s),
        "candidate_exits": _attr("candidate_exits")(s),
        "edge_kind": _attr("edge_kind")(s),
        "target_pre_materialized": _attr("target_pre_materialized")(s),
        "materialize_triggered": _attr("materialize_triggered")(s),
        "party_split_after": _attr("party_split_after")(s),
    },
)

SPAN_ROUTES[SPAN_MOVEMENT_UNRESOLVED] = SpanRoute(
    event_type="state_transition",
    component="movement",
    extract=lambda s: {
        "field": "pc_regions",
        "op": "movement.unresolved",
        "pc_name": _attr("pc_name")(s),
        "reason": _attr("reason")(s),
        "from_region": _attr("from_region")(s),
        "intent.direction": _attr("intent.direction")(s),
        "intent.exit_descriptor": _attr("intent.exit_descriptor")(s),
        "available_exits": _attr("available_exits")(s),
    },
)

# ---------------------------------------------------------------------------
# Context-manager helpers
# ---------------------------------------------------------------------------


@contextmanager
def movement_resolved_span(
    *,
    pc_name: str,
    from_region: str,
    to_region: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``movement.resolved`` INFO span for one PC's successful
    move. The handler writes the remaining lie-detector attributes
    (``resolved_via`` / ``candidate_exits`` / ``edge_kind`` /
    ``target_pre_materialized`` / ``materialize_triggered`` /
    ``party_split_after`` / ``intent.*``) onto the span."""
    with Span.open(
        SPAN_MOVEMENT_RESOLVED,
        {
            "pc_name": pc_name,
            "from_region": from_region,
            "to_region": to_region,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def movement_unresolved_span(
    *,
    pc_name: str,
    reason: str,
    from_region: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``movement.unresolved`` ERROR span for one PC's failed
    move (fail-loud, §Q4). Marks the span status ERROR so the GM panel
    surfaces it as a failure, never a silent stay-put. The handler writes
    ``intent.*`` + ``available_exits``."""
    with Span.open(
        SPAN_MOVEMENT_UNRESOLVED,
        {
            "pc_name": pc_name,
            "reason": reason,
            "from_region": from_region,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        span.set_status(Status(StatusCode.ERROR, reason))
        yield span


__all__ = [
    "SPAN_MOVEMENT_RESOLVED",
    "SPAN_MOVEMENT_UNRESOLVED",
    "movement_resolved_span",
    "movement_unresolved_span",
]
