"""Fate ruleset OTEL spans (ADR-144). The GM panel is the lie detector: a Fate
roll that fired emits ``fate.action_resolved`` carrying the full math."""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span


def fate_action_resolved_span(
    *,
    actor: str,
    skill_rating: int,
    dice: tuple[int, int, int, int],
    ladder_total: int,
    opposition: int,
    opposition_kind: str,
    shifts: int,
    tier: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.action_resolved`` — one Fate roll resolved."""
    attributes: dict[str, Any] = {
        "field": "action_resolved",
        "actor": actor,
        "skill_rating": skill_rating,
        "dice": ",".join(str(d) for d in dice),
        "ladder_total": ladder_total,
        "opposition": opposition,
        "opposition_kind": opposition_kind,
        "shifts": shifts,
        "tier": tier,
        **attrs,
    }
    with Span.open("fate.action_resolved", attributes, tracer_override=_tracer):
        pass


# --- F1b: fate-point economy + facet spans (GM panel = lie detector) ---------
# Registered as typed state_transition routes so the GM panel surfaces each
# economy delta and each stress/consequence mark in a typed tab (not just the
# always-on agent_span_close fan-out). Literal keys, no SPAN_* constants — the
# routing-completeness lint (tests/telemetry/test_routing_completeness.py) only
# inspects SPAN_* module constants, so these need no FLAT_ONLY entry.
SPAN_ROUTES["fate.fate_point.delta"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "fate_point_delta",
        "actor": (span.attributes or {}).get("actor", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "before": (span.attributes or {}).get("before", 0),
        "after": (span.attributes or {}).get("after", 0),
    },
)
SPAN_ROUTES["fate.aspect.invoked"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "aspect_invoked",
        "actor": (span.attributes or {}).get("actor", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
        "free": (span.attributes or {}).get("free", False),
        "mode": (span.attributes or {}).get("mode", ""),
        "fate_points_after": (span.attributes or {}).get("fate_points_after", 0),
    },
)
SPAN_ROUTES["fate.compel.offered"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "compel_offered",
        "actor": (span.attributes or {}).get("actor", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
    },
)
SPAN_ROUTES["fate.compel.accepted"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "compel_accepted",
        "actor": (span.attributes or {}).get("actor", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
        "fate_points_after": (span.attributes or {}).get("fate_points_after", 0),
    },
)
SPAN_ROUTES["fate.stress.applied"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "stress_applied",
        "actor": (span.attributes or {}).get("actor", ""),
        "track": (span.attributes or {}).get("track", ""),
        "box_value": (span.attributes or {}).get("box_value", 0),
    },
)
SPAN_ROUTES["fate.consequence.taken"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "consequence_taken",
        "actor": (span.attributes or {}).get("actor", ""),
        "level": (span.attributes or {}).get("level", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
    },
)


def fate_point_delta_span(
    *,
    actor: str,
    reason: str,
    before: int,
    after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.fate_point.delta`` — one fate-point change with its reason."""
    attributes: dict[str, Any] = {
        "field": "fate_point_delta",
        "actor": actor,
        "reason": reason,
        "before": before,
        "after": after,
        **attrs,
    }
    with Span.open("fate.fate_point.delta", attributes, tracer_override=_tracer):
        pass


def fate_aspect_invoked_span(
    *,
    actor: str,
    aspect: str,
    free: bool,
    mode: str,
    fate_points_after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.aspect.invoked`` — an aspect invoked for +2 or a reroll.
    ``free`` distinguishes a free invocation from a fate-point-paid one."""
    attributes: dict[str, Any] = {
        "field": "aspect_invoked",
        "actor": actor,
        "aspect": aspect,
        "free": free,
        "mode": mode,
        "fate_points_after": fate_points_after,
        **attrs,
    }
    with Span.open("fate.aspect.invoked", attributes, tracer_override=_tracer):
        pass


def fate_compel_offered_span(
    *,
    actor: str,
    aspect: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.compel.offered`` — the narrator proposed a compel (no economy
    change until accepted)."""
    attributes: dict[str, Any] = {
        "field": "compel_offered",
        "actor": actor,
        "aspect": aspect,
        **attrs,
    }
    with Span.open("fate.compel.offered", attributes, tracer_override=_tracer):
        pass


def fate_compel_accepted_span(
    *,
    actor: str,
    aspect: str,
    fate_points_after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.compel.accepted`` — a compel accepted (earns one fate point)."""
    attributes: dict[str, Any] = {
        "field": "compel_accepted",
        "actor": actor,
        "aspect": aspect,
        "fate_points_after": fate_points_after,
        **attrs,
    }
    with Span.open("fate.compel.accepted", attributes, tracer_override=_tracer):
        pass


def fate_stress_applied_span(
    *,
    actor: str,
    track: str,
    box_value: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.stress.applied`` — one stress box checked to absorb a hit."""
    attributes: dict[str, Any] = {
        "field": "stress_applied",
        "actor": actor,
        "track": track,
        "box_value": box_value,
        **attrs,
    }
    with Span.open("fate.stress.applied", attributes, tracer_override=_tracer):
        pass


def fate_consequence_taken_span(
    *,
    actor: str,
    level: str,
    aspect: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``fate.consequence.taken`` — a consequence slot filled (becomes an
    aspect). ``aspect`` is the consequence's free-text."""
    attributes: dict[str, Any] = {
        "field": "consequence_taken",
        "actor": actor,
        "level": level,
        "aspect": aspect,
        **attrs,
    }
    with Span.open("fate.consequence.taken", attributes, tracer_override=_tracer):
        pass


# --- F1c: conflict exchange spans (GM panel = lie detector) ------------------
SPAN_ROUTES["fate.exchange.committed"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "exchange_committed",
        "committed_actors": (span.attributes or {}).get("committed_actors", ""),
    },
)
SPAN_ROUTES["fate.exchange.order"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "exchange_order",
        "order": (span.attributes or {}).get("order", ""),
        "skill": (span.attributes or {}).get("skill", ""),
    },
)
SPAN_ROUTES["fate.exchange.resolved"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "exchange_resolved",
        "resolution_order": (span.attributes or {}).get("resolution_order", ""),
        "resolved": (span.attributes or {}).get("resolved", False),
        "round_number": (span.attributes or {}).get("round_number", 0),
    },
)
SPAN_ROUTES["fate.aspect.created"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "aspect_created",
        "actor": (span.attributes or {}).get("actor", ""),
        "aspect": (span.attributes or {}).get("aspect", ""),
        "free_invokes": (span.attributes or {}).get("free_invokes", 0),
    },
)
SPAN_ROUTES["fate.taken_out"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "taken_out",
        "actor": (span.attributes or {}).get("actor", ""),
        "by": (span.attributes or {}).get("by", ""),
        "shifts": (span.attributes or {}).get("shifts", 0),
    },
)
SPAN_ROUTES["fate.conceded"] = SpanRoute(
    event_type="state_transition",
    component="fate",
    extract=lambda span: {
        "field": "conceded",
        "actor": (span.attributes or {}).get("actor", ""),
        "fate_points_earned": (span.attributes or {}).get("fate_points_earned", 0),
    },
)


def fate_exchange_committed_span(
    *, committed_actors: str, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.exchange.committed`` — the sealed-commit barrier closed."""
    attributes: dict[str, Any] = {
        "field": "exchange_committed",
        "committed_actors": committed_actors,
        **attrs,
    }
    with Span.open("fate.exchange.committed", attributes, tracer_override=_tracer):
        pass


def fate_exchange_order_span(
    *, order: str, skill: str, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.exchange.order`` — the resolved turn order for this exchange,
    keyed by the initiative skill (Notice for physical, Empathy for mental conflicts)."""
    attributes: dict[str, Any] = {
        "field": "exchange_order",
        "order": order,
        "skill": skill,
        **attrs,
    }
    with Span.open("fate.exchange.order", attributes, tracer_override=_tracer):
        pass


def fate_exchange_resolved_span(
    *, resolution_order: str, resolved: bool, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.exchange.resolved`` — the exchange walk finished. ``resolved``
    is whether the confrontation itself ended this exchange."""
    attributes: dict[str, Any] = {
        "field": "exchange_resolved",
        "resolution_order": resolution_order,
        "resolved": resolved,
        **attrs,
    }
    with Span.open("fate.exchange.resolved", attributes, tracer_override=_tracer):
        pass


def fate_aspect_created_span(
    *, actor: str, aspect: str, free_invokes: int, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.aspect.created`` — create-advantage placed a situation aspect
    (or a boost) with ``free_invokes`` free invocations."""
    attributes: dict[str, Any] = {
        "field": "aspect_created",
        "actor": actor,
        "aspect": aspect,
        "free_invokes": free_invokes,
        **attrs,
    }
    with Span.open("fate.aspect.created", attributes, tracer_override=_tracer):
        pass


def fate_taken_out_span(
    *, actor: str, by: str, shifts: int, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.taken_out`` — an actor's stress+consequences could not absorb a
    hit and they are out of the conflict."""
    attributes: dict[str, Any] = {
        "field": "taken_out",
        "actor": actor,
        "by": by,
        "shifts": shifts,
        **attrs,
    }
    with Span.open("fate.taken_out", attributes, tracer_override=_tracer):
        pass


def fate_conceded_span(
    *, actor: str, fate_points_earned: int, _tracer: trace.Tracer | None = None, **attrs: Any
) -> None:
    """Emit ``fate.conceded`` — a player conceded (pre-roll), leaving on their
    terms and earning fate points."""
    attributes: dict[str, Any] = {
        "field": "conceded",
        "actor": actor,
        "fate_points_earned": fate_points_earned,
        **attrs,
    }
    with Span.open("fate.conceded", attributes, tracer_override=_tracer):
        pass


__all__ = [
    "fate_action_resolved_span",
    "fate_aspect_created_span",
    "fate_aspect_invoked_span",
    "fate_compel_accepted_span",
    "fate_compel_offered_span",
    "fate_conceded_span",
    "fate_consequence_taken_span",
    "fate_exchange_committed_span",
    "fate_exchange_order_span",
    "fate_exchange_resolved_span",
    "fate_point_delta_span",
    "fate_stress_applied_span",
    "fate_taken_out_span",
]
