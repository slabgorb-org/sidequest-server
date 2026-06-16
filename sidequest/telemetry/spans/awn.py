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


SPAN_AWN_SAINT_APPLIED = "awn.saint.applied"
SPAN_ROUTES[SPAN_AWN_SAINT_APPLIED] = SpanRoute(
    event_type="state_transition",
    component="awn",
    extract=lambda span: {
        "field": "saint",
        "actor": (span.attributes or {}).get("actor", ""),
        "saint_id": (span.attributes or {}).get("saint_id", ""),
        "drawback": (span.attributes or {}).get("drawback", ""),
        "bundle_count": (span.attributes or {}).get("bundle_count", 0),
        "mp_base": (span.attributes or {}).get("mp_base", 0),
        "mp_from_drawback": (span.attributes or {}).get("mp_from_drawback", 0),
        "mp_spent": (span.attributes or {}).get("mp_spent", 0),
        "mp_remaining": (span.attributes or {}).get("mp_remaining", 0),
    },
)


def awn_saint_applied_span(
    *,
    actor: str,
    saint_id: str,
    drawback: str,
    bundle_count: int,
    mp_base: int,
    mp_from_drawback: int,
    mp_spent: int,
    mp_remaining: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Saint-Marked chargen preset applied (story 103-1, build plan D-D).

    The MP arithmetic must be auditable from the GM panel alone — the span
    carries every term of ``mp_base + mp_from_drawback - mp_spent``.
    """
    attributes: dict[str, Any] = {
        "field": "saint",
        "actor": actor,
        "saint_id": saint_id,
        "drawback": drawback,
        "bundle_count": bundle_count,
        "mp_base": mp_base,
        "mp_from_drawback": mp_from_drawback,
        "mp_spent": mp_spent,
        "mp_remaining": mp_remaining,
        **attrs,
    }
    with Span.open(SPAN_AWN_SAINT_APPLIED, attributes, tracer_override=_tracer):
        pass


SPAN_AWN_STOCK_APPLIED = "awn.stock.applied"
SPAN_ROUTES[SPAN_AWN_STOCK_APPLIED] = SpanRoute(
    event_type="state_transition",
    component="awn",
    extract=lambda span: {
        "field": "stock",
        "actor": (span.attributes or {}).get("actor", ""),
        "stock_id": (span.attributes or {}).get("stock_id", ""),
        "granted_count": (span.attributes or {}).get("granted_count", 0),
        "attr_mods": (span.attributes or {}).get("attr_mods", ""),
        "ac": (span.attributes or {}).get("ac", ""),
        "move": (span.attributes or {}).get("move", ""),
        "trauma_target_mod": (span.attributes or {}).get("trauma_target_mod", 0),
        "saint_id": (span.attributes or {}).get("saint_id", ""),
    },
)


def awn_stock_applied_span(
    *,
    actor: str,
    stock_id: str,
    granted_count: int,
    attr_mods: str,
    trauma_target_mod: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Stock trait set applied at chargen (story 103-2, build plan D-D).

    The applied trait deltas must be auditable from the GM panel alone —
    attr mods ride as a flat signed string ("STR +1, WIS -1"); ac/move
    appear only when the stock overrides them (OTEL attrs cannot be None).
    """
    attributes: dict[str, Any] = {
        "field": "stock",
        "actor": actor,
        "stock_id": stock_id,
        "granted_count": granted_count,
        "attr_mods": attr_mods,
        "trauma_target_mod": trauma_target_mod,
        **attrs,
    }
    with Span.open(SPAN_AWN_STOCK_APPLIED, attributes, tracer_override=_tracer):
        pass


SPAN_AWN_MUTATION_STIGMA = "awn.mutation.stigma"
SPAN_ROUTES[SPAN_AWN_MUTATION_STIGMA] = SpanRoute(
    event_type="state_transition",
    component="awn",
    extract=lambda span: {
        "field": "mutation_stigma",
        "actor": (span.attributes or {}).get("actor", ""),
        "body_part": (span.attributes or {}).get("body_part", ""),
        "nature": (span.attributes or {}).get("nature", ""),
        "flavor": (span.attributes or {}).get("flavor", ""),
        "concealable": (span.attributes or {}).get("concealable", False),
    },
)


def awn_mutation_stigma_span(
    *,
    actor: str,
    body_part: str,
    nature: str,
    flavor: str,
    concealable: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    attributes: dict[str, Any] = {
        "field": "mutation_stigma",
        "actor": actor,
        "body_part": body_part,
        "nature": nature,
        "flavor": flavor,
        "concealable": concealable,
        **attrs,
    }
    with Span.open(SPAN_AWN_MUTATION_STIGMA, attributes, tracer_override=_tracer):
        pass
