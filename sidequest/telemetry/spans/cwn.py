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


SPAN_CWN_TRAUMA_ROLL = "cwn.trauma.roll"
SPAN_ROUTES[SPAN_CWN_TRAUMA_ROLL] = SpanRoute(
    event_type="state_transition",
    component="cwn",
    extract=lambda span: {
        "field": "trauma",
        "actor": (span.attributes or {}).get("actor", ""),
        "weapon_die": (span.attributes or {}).get("weapon_die", ""),
        "roll": (span.attributes or {}).get("roll", 0),
        "target": (span.attributes or {}).get("target", 0),
        "traumatic": (span.attributes or {}).get("traumatic", False),
        "rating": (span.attributes or {}).get("rating", 1),
        "base": (span.attributes or {}).get("base", 0),
        "final": (span.attributes or {}).get("final", 0),
    },
)

SPAN_CWN_SHOCK_APPLIED = "cwn.shock.applied"
SPAN_ROUTES[SPAN_CWN_SHOCK_APPLIED] = SpanRoute(
    event_type="state_transition",
    component="cwn",
    extract=lambda span: {
        "field": "shock",
        "actor": (span.attributes or {}).get("actor", ""),
        "amount": (span.attributes or {}).get("amount", 0),
        "melee_ac": (span.attributes or {}).get("melee_ac", 0),
        "shock_rating": (span.attributes or {}).get("shock_rating", 0),
        "shock_ac": (span.attributes or {}).get("shock_ac", 0),
    },
)

SPAN_CWN_MORTAL_INJURY_DECLARED = "cwn.mortal_injury.declared"
SPAN_ROUTES[SPAN_CWN_MORTAL_INJURY_DECLARED] = SpanRoute(
    event_type="state_transition",
    component="cwn",
    extract=lambda span: {
        "field": "mortal_injury",
        "actor": (span.attributes or {}).get("actor", ""),
        "rounds_to_die": (span.attributes or {}).get("rounds_to_die", 0),
    },
)

SPAN_CWN_MAJOR_INJURY_ROLL = "cwn.major_injury.roll"
SPAN_ROUTES[SPAN_CWN_MAJOR_INJURY_ROLL] = SpanRoute(
    event_type="state_transition",
    component="cwn",
    extract=lambda span: {
        "field": "major_injury",
        "actor": (span.attributes or {}).get("actor", ""),
        "save_made": (span.attributes or {}).get("save_made", True),
        "roll": (span.attributes or {}).get("roll", 0),
        "text": (span.attributes or {}).get("text", ""),
    },
)


def cwn_trauma_roll_span(
    *,
    actor: str,
    weapon_die: str,
    roll: int,
    target: int,
    traumatic: bool,
    rating: int,
    base: int,
    final: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a cwn.trauma.roll span (lie-detector for CWN trauma threshold check)."""
    attributes: dict[str, Any] = {
        "field": "trauma",
        "actor": actor,
        "weapon_die": weapon_die,
        "roll": roll,
        "target": target,
        "traumatic": traumatic,
        "rating": rating,
        "base": base,
        "final": final,
        **attrs,
    }
    with Span.open(SPAN_CWN_TRAUMA_ROLL, attributes, tracer_override=_tracer):
        pass


def cwn_shock_applied_span(
    *,
    actor: str,
    amount: int,
    melee_ac: int,
    shock_rating: int,
    shock_ac: int | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a cwn.shock.applied span (lie-detector for CWN shock damage application)."""
    attributes: dict[str, Any] = {
        "field": "shock",
        "actor": actor,
        "amount": amount,
        "melee_ac": melee_ac,
        "shock_rating": shock_rating,
        "shock_ac": shock_ac,
        **attrs,
    }
    with Span.open(SPAN_CWN_SHOCK_APPLIED, attributes, tracer_override=_tracer):
        pass


def cwn_mortal_injury_declared_span(
    *,
    actor: str,
    rounds_to_die: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a cwn.mortal_injury.declared span (lie-detector for CWN mortal wound declaration)."""
    attributes: dict[str, Any] = {
        "field": "mortal_injury",
        "actor": actor,
        "rounds_to_die": rounds_to_die,
        **attrs,
    }
    with Span.open(SPAN_CWN_MORTAL_INJURY_DECLARED, attributes, tracer_override=_tracer):
        pass


def cwn_major_injury_roll_span(
    *,
    actor: str,
    save_made: bool,
    roll: int,
    text: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a cwn.major_injury.roll span (lie-detector for CWN major injury table roll)."""
    attributes: dict[str, Any] = {
        "field": "major_injury",
        "actor": actor,
        "save_made": save_made,
        "roll": roll,
        "text": text,
        **attrs,
    }
    with Span.open(SPAN_CWN_MAJOR_INJURY_ROLL, attributes, tracer_override=_tracer):
        pass


SPAN_CWN_HACKING_SECURITY_CHECK = "cwn.hacking.security_check"
SPAN_ROUTES[SPAN_CWN_HACKING_SECURITY_CHECK] = SpanRoute(
    event_type="state_transition",
    component="cwn",
    extract=lambda span: {
        "field": "hacking",
        "actor": (span.attributes or {}).get("actor", ""),
        "verb": (span.attributes or {}).get("verb", ""),
        "tier": (span.attributes or {}).get("tier", ""),
        "base_dc": (span.attributes or {}).get("base_dc", 0),
        "alert_modifier": (span.attributes or {}).get("alert_modifier", 0),
        "effective_dc": (span.attributes or {}).get("effective_dc", 0),
        "result": (span.attributes or {}).get("result", ""),
    },
)


def cwn_hacking_security_check_span(
    *,
    actor: str,
    verb: str,
    tier: str,
    base_dc: int,
    alert_modifier: int,
    effective_dc: int,
    result: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a cwn.hacking.security_check span (lie-detector for CWN hacking).

    Fires on EVERY resolved net_run verb so the GM panel sees engaged and
    unengaged rolls alike. Point mutation, not a span of work — opens and
    immediately closes so WatcherSpanProcessor routes it to the state_transition
    feed.
    """
    attributes: dict[str, Any] = {
        "field": "hacking",
        "actor": actor,
        "verb": verb,
        "tier": tier,
        "base_dc": base_dc,
        "alert_modifier": alert_modifier,
        "effective_dc": effective_dc,
        "result": result,
        **attrs,
    }
    with Span.open(SPAN_CWN_HACKING_SECURITY_CHECK, attributes, tracer_override=_tracer):
        pass
