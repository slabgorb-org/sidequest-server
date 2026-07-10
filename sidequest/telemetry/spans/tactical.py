"""Tactical-grid adjudication spans (ADR-096 v2, Track C2).

The GM panel is the lie detector: every grid adjudication must be provably
engaged vs improvised. These spans mirror into the turn_telemetry sink via
``publish_event`` (the ``spans/movement.py`` pattern) so a firing engine does
not read as DEAD in saves — a bare ``Span.open`` reaches only Jaeger/live panel.
"""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from sidequest.telemetry.watcher_hub import publish_event

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_TACTICAL_MOVE_VALIDATED = "tactical.move.validated"
SPAN_TACTICAL_MOVE_DENIED = "tactical.move.denied"
SPAN_TACTICAL_AOE_CELLS = "tactical.aoe.cells"
SPAN_TACTICAL_ENFORCEMENT_SKIPPED = "tactical.enforcement.skipped"
SPAN_TACTICAL_POSITIONS_SEATED = "tactical.positions.seated"
SPAN_TACTICAL_ZONE_PROJECTED = "tactical.zone.projected"
SPAN_TACTICAL_ZONE_MOVE = "tactical.zone.move"


def _attr(field: str):
    return lambda span, f=field: (span.attributes or {}).get(f)


SPAN_ROUTES[SPAN_TACTICAL_MOVE_VALIDATED] = SpanRoute(
    event_type="state_transition",
    component="tactical",
    extract=lambda s: {
        "field": "encounter",
        "op": "tactical.move.validated",
        "actor": _attr("actor")(s),
        "cells_spent": _attr("cells_spent")(s),
        "cells_budget": _attr("cells_budget")(s),
        "from_cell": _attr("from_cell")(s),
        "to_cell": _attr("to_cell")(s),
    },
)
SPAN_ROUTES[SPAN_TACTICAL_MOVE_DENIED] = SpanRoute(
    event_type="state_transition",
    component="tactical",
    extract=lambda s: {
        "field": "encounter",
        "op": "tactical.move.denied",
        "actor": _attr("actor")(s),
        "cells_spent": _attr("cells_spent")(s),
        "cells_budget": _attr("cells_budget")(s),
        "reason": _attr("reason")(s),
    },
)
SPAN_ROUTES[SPAN_TACTICAL_AOE_CELLS] = SpanRoute(
    event_type="state_transition",
    component="tactical",
    extract=lambda s: {
        "field": "encounter",
        "op": "tactical.aoe.cells",
        "actor": _attr("actor")(s),
        "template": _attr("template")(s),
        "cell_count": _attr("cell_count")(s),
        "radius": _attr("radius")(s),
        "cells_json": _attr("cells_json")(s),
    },
)
SPAN_ROUTES[SPAN_TACTICAL_ENFORCEMENT_SKIPPED] = SpanRoute(
    event_type="state_transition",
    component="tactical",
    extract=lambda s: {
        "field": "encounter",
        "op": "tactical.enforcement.skipped",
        "actor": _attr("actor")(s),
        "reason": _attr("reason")(s),
    },
)
SPAN_ROUTES[SPAN_TACTICAL_POSITIONS_SEATED] = SpanRoute(
    event_type="state_transition",
    component="tactical",
    extract=lambda s: {
        "field": "encounter",
        "op": "tactical.positions.seated",
        "seated_count": _attr("seated_count")(s),
        "room_id": _attr("room_id")(s),
    },
)
SPAN_ROUTES[SPAN_TACTICAL_ZONE_PROJECTED] = SpanRoute(
    event_type="state_transition",
    component="tactical",
    extract=lambda s: {
        "field": "encounter",
        "op": "tactical.zone.projected",
        "zone_count": _attr("zone_count")(s),
        "room_id": _attr("room_id")(s),
    },
)
SPAN_ROUTES[SPAN_TACTICAL_ZONE_MOVE] = SpanRoute(
    event_type="state_transition",
    component="tactical",
    extract=lambda s: {
        "field": "encounter",
        "op": "tactical.zone.move",
        "actor": _attr("actor")(s),
        "from_zone": _attr("from_zone")(s),
        "to_zone": _attr("to_zone")(s),
        "free": _attr("free")(s),
        "requires_overcome": _attr("requires_overcome")(s),
    },
)


def _mirror(span_name: str, span: trace.Span) -> None:
    """Mirror a finished tactical span into the turn_telemetry sink. Skips
    silently for a NonRecordingSpan (no ``attributes``) — a telemetry side
    channel must never crash an adjudication (mirrors ``movement.py``)."""
    route = SPAN_ROUTES.get(span_name)
    if route is None or not hasattr(span, "attributes"):
        return
    publish_event(route.event_type, route.extract(span), component=route.component)


@contextmanager
def tactical_move_validated_span(
    *,
    actor: str,
    cells_spent: int,
    cells_budget: int,
    from_cell: tuple[int, int],
    to_cell: tuple[int, int],
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TACTICAL_MOVE_VALIDATED,
        {
            "actor": actor,
            "cells_spent": cells_spent,
            "cells_budget": cells_budget,
            "from_cell": list(from_cell),
            "to_cell": list(to_cell),
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror(SPAN_TACTICAL_MOVE_VALIDATED, span)


@contextmanager
def tactical_move_denied_span(
    *,
    actor: str,
    cells_spent: int,
    cells_budget: int,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    from opentelemetry.trace import Status, StatusCode

    with Span.open(
        SPAN_TACTICAL_MOVE_DENIED,
        {
            "actor": actor,
            "cells_spent": cells_spent,
            "cells_budget": cells_budget,
            "reason": reason,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        span.set_status(Status(StatusCode.ERROR, reason))
        yield span
    _mirror(SPAN_TACTICAL_MOVE_DENIED, span)


@contextmanager
def tactical_aoe_cells_span(
    *,
    actor: str,
    template: str,
    cell_count: int,
    radius: int,
    cells: list[tuple[int, int]] | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TACTICAL_AOE_CELLS,
        {
            "actor": actor,
            "template": template,
            "cell_count": cell_count,
            "radius": radius,
            "cells_json": _json.dumps([list(c) for c in (cells or [])]),
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror(SPAN_TACTICAL_AOE_CELLS, span)


@contextmanager
def tactical_enforcement_skipped_span(
    *,
    actor: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TACTICAL_ENFORCEMENT_SKIPPED,
        {"actor": actor, "reason": reason, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror(SPAN_TACTICAL_ENFORCEMENT_SKIPPED, span)


@contextmanager
def tactical_positions_seated_span(
    *,
    seated_count: int,
    room_id: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TACTICAL_POSITIONS_SEATED,
        {"seated_count": seated_count, "room_id": room_id, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror(SPAN_TACTICAL_POSITIONS_SEATED, span)


@contextmanager
def tactical_zone_projected_span(
    *,
    zone_count: int,
    room_id: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TACTICAL_ZONE_PROJECTED,
        {"zone_count": zone_count, "room_id": room_id, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror(SPAN_TACTICAL_ZONE_PROJECTED, span)


@contextmanager
def tactical_zone_move_span(
    *,
    actor: str,
    from_zone: str,
    to_zone: str,
    free: bool,
    requires_overcome: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_TACTICAL_ZONE_MOVE,
        {
            "actor": actor,
            "from_zone": from_zone,
            "to_zone": to_zone,
            "free": free,
            "requires_overcome": requires_overcome,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
    _mirror(SPAN_TACTICAL_ZONE_MOVE, span)


__all__ = [
    "SPAN_TACTICAL_AOE_CELLS",
    "SPAN_TACTICAL_ENFORCEMENT_SKIPPED",
    "SPAN_TACTICAL_MOVE_DENIED",
    "SPAN_TACTICAL_MOVE_VALIDATED",
    "SPAN_TACTICAL_POSITIONS_SEATED",
    "SPAN_TACTICAL_ZONE_MOVE",
    "SPAN_TACTICAL_ZONE_PROJECTED",
    "tactical_aoe_cells_span",
    "tactical_enforcement_skipped_span",
    "tactical_move_denied_span",
    "tactical_move_validated_span",
    "tactical_positions_seated_span",
    "tactical_zone_move_span",
    "tactical_zone_projected_span",
]
