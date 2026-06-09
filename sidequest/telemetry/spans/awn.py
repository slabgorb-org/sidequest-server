"""AWN-specific OTEL spans (mutation subsystem). GM panel = lie detector."""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_AWN_MUTATION_ACQUIRED = "awn.mutation.acquired"
SPAN_ROUTES[SPAN_AWN_MUTATION_ACQUIRED] = SpanRoute(
    event_type="state_transition",
    component="awn",
    extract=lambda span: {
        "field": "mutation",
        "actor": (span.attributes or {}).get("actor", ""),
        "mutation_id": (span.attributes or {}).get("mutation_id", ""),
        "source": (span.attributes or {}).get("source", ""),
        "roll": (span.attributes or {}).get("roll", 0),
        "mp_delta": (span.attributes or {}).get("mp_delta", 0),
        "mp_remaining": (span.attributes or {}).get("mp_remaining", 0),
    },
)


def awn_mutation_acquired_span(
    *,
    actor: str,
    mutation_id: str,
    source: str,
    roll: int,
    mp_delta: int,
    mp_remaining: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    attributes: dict[str, Any] = {
        "field": "mutation",
        "actor": actor,
        "mutation_id": mutation_id,
        "source": source,
        "roll": roll,
        "mp_delta": mp_delta,
        "mp_remaining": mp_remaining,
        **attrs,
    }
    with Span.open(SPAN_AWN_MUTATION_ACQUIRED, attributes, tracer_override=_tracer):
        pass


SPAN_AWN_MUTATION_USED = "awn.mutation.used"
SPAN_ROUTES[SPAN_AWN_MUTATION_USED] = SpanRoute(
    event_type="state_transition",
    component="awn",
    extract=lambda span: {
        "field": "mutation",
        "actor": (span.attributes or {}).get("actor", ""),
        "mutation_id": (span.attributes or {}).get("mutation_id", ""),
        "strain_cost": (span.attributes or {}).get("strain_cost", 0),
        "uses_remaining": (span.attributes or {}).get("uses_remaining", -1),
        "save_stat": (span.attributes or {}).get("save_stat", ""),
        "save_result": (span.attributes or {}).get("save_result", ""),
    },
)


def awn_mutation_used_span(
    *,
    actor: str,
    mutation_id: str,
    strain_cost: int,
    uses_remaining: int,
    save_stat: str = "",
    save_result: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    attributes: dict[str, Any] = {
        "field": "mutation",
        "actor": actor,
        "mutation_id": mutation_id,
        "strain_cost": strain_cost,
        "uses_remaining": uses_remaining,
        "save_stat": save_stat,
        "save_result": save_result,
        **attrs,
    }
    with Span.open(SPAN_AWN_MUTATION_USED, attributes, tracer_override=_tracer):
        pass


SPAN_AWN_MUTATION_REFUSED = "awn.mutation.refused"
SPAN_ROUTES[SPAN_AWN_MUTATION_REFUSED] = SpanRoute(
    event_type="state_transition",
    component="awn",
    extract=lambda span: {
        "field": "mutation",
        "actor": (span.attributes or {}).get("actor", ""),
        "mutation_id": (span.attributes or {}).get("mutation_id", ""),
        "reason": (span.attributes or {}).get("reason", ""),
    },
)


def awn_mutation_refused_span(
    *,
    actor: str,
    mutation_id: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    attributes: dict[str, Any] = {
        "field": "mutation",
        "actor": actor,
        "mutation_id": mutation_id,
        "reason": reason,
        **attrs,
    }
    with Span.open(SPAN_AWN_MUTATION_REFUSED, attributes, tracer_override=_tracer):
        pass


SPAN_AWN_MUTATION_MP_SPEND = "awn.mutation.mp_spend"
SPAN_ROUTES[SPAN_AWN_MUTATION_MP_SPEND] = SpanRoute(
    event_type="state_transition",
    component="awn",
    extract=lambda span: {
        "field": "mutation_mp",
        "actor": (span.attributes or {}).get("actor", ""),
        "spend_kind": (span.attributes or {}).get("spend_kind", ""),
        "cost": (span.attributes or {}).get("cost", 0),
        "mp_remaining": (span.attributes or {}).get("mp_remaining", 0),
    },
)


def awn_mutation_mp_spend_span(
    *,
    actor: str,
    spend_kind: str,
    cost: int,
    mp_remaining: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    attributes: dict[str, Any] = {
        "field": "mutation_mp",
        "actor": actor,
        "spend_kind": spend_kind,
        "cost": cost,
        "mp_remaining": mp_remaining,
        **attrs,
    }
    with Span.open(SPAN_AWN_MUTATION_MP_SPEND, attributes, tracer_override=_tracer):
        pass
