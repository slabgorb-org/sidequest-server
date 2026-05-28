"""CWN-specific OTEL spans. The GM panel is the lie detector for engine truth."""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_CWN_SYSTEM_STRAIN_DELTA = "cwn.system_strain.delta"
SPAN_ROUTES[SPAN_CWN_SYSTEM_STRAIN_DELTA] = SpanRoute(
    event_type="state_transition",
    component="cwn",
    extract=lambda span: {
        "field": "system_strain",
        "actor": (span.attributes or {}).get("actor", ""),
        "source": (span.attributes or {}).get("source", ""),
        "amount": (span.attributes or {}).get("amount", 0),
        "new_total": (span.attributes or {}).get("new_total", 0),
        "max": (span.attributes or {}).get("max", 0),
        "applied": (span.attributes or {}).get("applied", True),
    },
)


def cwn_system_strain_delta_span(
    *,
    actor: str,
    source: str,
    amount: int,
    new_total: int,
    max: int,
    applied: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a cwn.system_strain.delta span (lie-detector for CWN System Strain).

    Not a context manager — the strain delta is a point mutation, not a span
    of work. Opens and immediately closes the span so the WatcherSpanProcessor
    can route it to the GM panel's state_transition feed.
    """
    attributes: dict[str, Any] = {
        "field": "system_strain",
        "actor": actor,
        "source": source,
        "amount": amount,
        "new_total": new_total,
        "max": max,
        "applied": applied,
        **attrs,
    }
    with Span.open(SPAN_CWN_SYSTEM_STRAIN_DELTA, attributes, tracer_override=_tracer):
        pass
