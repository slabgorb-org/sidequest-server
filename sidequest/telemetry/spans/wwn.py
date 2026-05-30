"""WWN-specific OTEL spans. The GM panel is the lie detector for engine truth.

Copied from telemetry/spans/cwn.py (the WWN lethality layer is the shared
"Without Number" core) with the cwn->wwn namespace rename and the hacking span
dropped (WWN has no cyberspace). Duplication is intentional per the WWN spec §2.1.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_WWN_SYSTEM_STRAIN_DELTA = "wwn.system_strain.delta"
SPAN_ROUTES[SPAN_WWN_SYSTEM_STRAIN_DELTA] = SpanRoute(
    event_type="state_transition",
    component="wwn",
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


def wwn_system_strain_delta_span(
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
    """Emit a wwn.system_strain.delta span (lie-detector for WWN System Strain)."""
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
    with Span.open(SPAN_WWN_SYSTEM_STRAIN_DELTA, attributes, tracer_override=_tracer):
        pass


SPAN_WWN_TRAUMA_ROLL = "wwn.trauma.roll"
SPAN_ROUTES[SPAN_WWN_TRAUMA_ROLL] = SpanRoute(
    event_type="state_transition",
    component="wwn",
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

SPAN_WWN_SHOCK_APPLIED = "wwn.shock.applied"
SPAN_ROUTES[SPAN_WWN_SHOCK_APPLIED] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "shock",
        "actor": (span.attributes or {}).get("actor", ""),
        "amount": (span.attributes or {}).get("amount", 0),
        "melee_ac": (span.attributes or {}).get("melee_ac", 0),
        "shock_rating": (span.attributes or {}).get("shock_rating", 0),
        "shock_ac": (span.attributes or {}).get("shock_ac", 0),
    },
)

SPAN_WWN_MORTAL_INJURY_DECLARED = "wwn.mortal_injury.declared"
SPAN_ROUTES[SPAN_WWN_MORTAL_INJURY_DECLARED] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "mortal_injury",
        "actor": (span.attributes or {}).get("actor", ""),
        "rounds_to_die": (span.attributes or {}).get("rounds_to_die", 0),
    },
)

SPAN_WWN_MAJOR_INJURY_ROLL = "wwn.major_injury.roll"
SPAN_ROUTES[SPAN_WWN_MAJOR_INJURY_ROLL] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "major_injury",
        "actor": (span.attributes or {}).get("actor", ""),
        "save_made": (span.attributes or {}).get("save_made", True),
        "roll": (span.attributes or {}).get("roll", 0),
        "text": (span.attributes or {}).get("text", ""),
    },
)


def wwn_trauma_roll_span(
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
    """Emit a wwn.trauma.roll span (lie-detector for WWN trauma threshold check)."""
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
    with Span.open(SPAN_WWN_TRAUMA_ROLL, attributes, tracer_override=_tracer):
        pass


def wwn_shock_applied_span(
    *,
    actor: str,
    amount: int,
    melee_ac: int,
    shock_rating: int,
    shock_ac: int | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.shock.applied span (lie-detector for WWN shock damage application)."""
    attributes: dict[str, Any] = {
        "field": "shock",
        "actor": actor,
        "amount": amount,
        "melee_ac": melee_ac,
        "shock_rating": shock_rating,
        "shock_ac": shock_ac,
        **attrs,
    }
    with Span.open(SPAN_WWN_SHOCK_APPLIED, attributes, tracer_override=_tracer):
        pass


def wwn_mortal_injury_declared_span(
    *,
    actor: str,
    rounds_to_die: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.mortal_injury.declared span (lie-detector for WWN mortal wound declaration)."""
    attributes: dict[str, Any] = {
        "field": "mortal_injury",
        "actor": actor,
        "rounds_to_die": rounds_to_die,
        **attrs,
    }
    with Span.open(SPAN_WWN_MORTAL_INJURY_DECLARED, attributes, tracer_override=_tracer):
        pass


def wwn_major_injury_roll_span(
    *,
    actor: str,
    save_made: bool,
    roll: int,
    text: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.major_injury.roll span (lie-detector for WWN major injury table roll)."""
    attributes: dict[str, Any] = {
        "field": "major_injury",
        "actor": actor,
        "save_made": save_made,
        "roll": roll,
        "text": text,
        **attrs,
    }
    with Span.open(SPAN_WWN_MAJOR_INJURY_ROLL, attributes, tracer_override=_tracer):
        pass


# ---------------------------------------------------------------------------
# Magic spans
# ---------------------------------------------------------------------------

SPAN_WWN_SPELL_CAST = "wwn.spell.cast"
SPAN_ROUTES[SPAN_WWN_SPELL_CAST] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "spell_cast",
        "actor": (span.attributes or {}).get("actor", ""),
        "spell_id": (span.attributes or {}).get("spell_id", ""),
        "level": (span.attributes or {}).get("level", 0),
        "refused": (span.attributes or {}).get("refused", False),
        "casts_remaining": (span.attributes or {}).get("casts_remaining", 0),
        "save": (span.attributes or {}).get("save", ""),
        # save_made is OMITTED from the span attributes when no save was resolved
        # (None) — the GM panel must read "unresolved", never a misleading False.
        "save_made": (span.attributes or {}).get("save_made", None),
        "damage": (span.attributes or {}).get("damage", ""),
    },
)

SPAN_WWN_EFFORT_COMMIT = "wwn.effort.commit"
SPAN_ROUTES[SPAN_WWN_EFFORT_COMMIT] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "effort_commit",
        "actor": (span.attributes or {}).get("actor", ""),
        "source": (span.attributes or {}).get("source", ""),
        "points": (span.attributes or {}).get("points", 0),
        "duration": (span.attributes or {}).get("duration", ""),
        "available": (span.attributes or {}).get("available", 0),
        "applied": (span.attributes or {}).get("applied", True),
    },
)

SPAN_WWN_EFFORT_RECLAIM = "wwn.effort.reclaim"
SPAN_ROUTES[SPAN_WWN_EFFORT_RECLAIM] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "effort_reclaim",
        "actor": (span.attributes or {}).get("actor", ""),
        "source": (span.attributes or {}).get("source", ""),
        "points": (span.attributes or {}).get("points", 0),
        "trigger": (span.attributes or {}).get("trigger", ""),
        "available": (span.attributes or {}).get("available", 0),
    },
)

SPAN_WWN_KILLING_BLOW = "wwn.killing_blow"
SPAN_ROUTES[SPAN_WWN_KILLING_BLOW] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "killing_blow",
        "actor": (span.attributes or {}).get("actor", ""),
        "level": (span.attributes or {}).get("level", 0),
        "bonus": (span.attributes or {}).get("bonus", 0),
        "base": (span.attributes or {}).get("base", 0),
        "total": (span.attributes or {}).get("total", 0),
    },
)

SPAN_WWN_VETERANS_LUCK = "wwn.veterans_luck"
SPAN_ROUTES[SPAN_WWN_VETERANS_LUCK] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "veterans_luck",
        "actor": (span.attributes or {}).get("actor", ""),
        "mode": (span.attributes or {}).get("mode", ""),
        "applied": (span.attributes or {}).get("applied", False),
    },
)


def wwn_spell_cast_span(
    *,
    actor: str,
    spell_id: str,
    level: int,
    refused: bool,
    casts_remaining: int,
    save: str,
    save_made: bool | None,
    damage: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.spell.cast span (lie-detector for WWN spell casting).

    ``save_made`` is ``None`` when NO save was resolved (no-save spell, or a save
    spell with no defender/stats). In that case the attribute is OMITTED entirely
    rather than coerced to False — OTEL attributes must not carry a misleading
    bool that tells the GM panel the defender "failed" a save that never rolled.
    """
    attributes: dict[str, Any] = {
        "field": "spell_cast",
        "actor": actor,
        "spell_id": spell_id,
        "level": level,
        "refused": refused,
        "casts_remaining": casts_remaining,
        "save": save,
        "damage": damage,
        **attrs,
    }
    if save_made is not None:
        attributes["save_made"] = save_made
    with Span.open(SPAN_WWN_SPELL_CAST, attributes, tracer_override=_tracer):
        pass


def wwn_effort_commit_span(
    *,
    actor: str,
    source: str,
    points: int,
    duration: str,
    available: int,
    applied: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.effort.commit span (lie-detector for WWN effort commitment)."""
    attributes: dict[str, Any] = {
        "field": "effort_commit",
        "actor": actor,
        "source": source,
        "points": points,
        "duration": duration,
        "available": available,
        "applied": applied,
        **attrs,
    }
    with Span.open(SPAN_WWN_EFFORT_COMMIT, attributes, tracer_override=_tracer):
        pass


def wwn_effort_reclaim_span(
    *,
    actor: str,
    source: str,
    points: int,
    trigger: str,
    available: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.effort.reclaim span (lie-detector for WWN effort reclamation)."""
    attributes: dict[str, Any] = {
        "field": "effort_reclaim",
        "actor": actor,
        "source": source,
        "points": points,
        "trigger": trigger,
        "available": available,
        **attrs,
    }
    with Span.open(SPAN_WWN_EFFORT_RECLAIM, attributes, tracer_override=_tracer):
        pass


def wwn_killing_blow_span(
    *,
    actor: str,
    level: int,
    bonus: int,
    base: int,
    total: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.killing_blow span (lie-detector for WWN Killing Blow bonus damage)."""
    attributes: dict[str, Any] = {
        "field": "killing_blow",
        "actor": actor,
        "level": level,
        "bonus": bonus,
        "base": base,
        "total": total,
        **attrs,
    }
    with Span.open(SPAN_WWN_KILLING_BLOW, attributes, tracer_override=_tracer):
        pass


def wwn_veterans_luck_span(
    *,
    actor: str,
    mode: str,
    applied: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.veterans_luck span (lie-detector for WWN Veteran's Luck activation)."""
    attributes: dict[str, Any] = {
        "field": "veterans_luck",
        "actor": actor,
        "mode": mode,
        "applied": applied,
        **attrs,
    }
    with Span.open(SPAN_WWN_VETERANS_LUCK, attributes, tracer_override=_tracer):
        pass


# ---------------------------------------------------------------------------
# Long rest span (Plan 3 — party-wide Effort reclaim + casts refresh)
# ---------------------------------------------------------------------------

SPAN_WWN_LONG_REST = "wwn.long_rest"
SPAN_ROUTES[SPAN_WWN_LONG_REST] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "long_rest",
        "actor": (span.attributes or {}).get("actor", ""),
        "day_effort_reclaimed": (span.attributes or {}).get("day_effort_reclaimed", False),
        "casts_refreshed_to": (span.attributes or {}).get("casts_refreshed_to", 0),
        "reprepared": (span.attributes or {}).get("reprepared", False),
        "comfortable": (span.attributes or {}).get("comfortable", True),
    },
)


def wwn_long_rest_span(
    *,
    actor: str,
    day_effort_reclaimed: bool,
    casts_refreshed_to: int,
    reprepared: bool,
    comfortable: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.long_rest span (lie-detector for WWN long rest per PC)."""
    attributes: dict[str, Any] = {
        "field": "long_rest",
        "actor": actor,
        "day_effort_reclaimed": day_effort_reclaimed,
        "casts_refreshed_to": casts_refreshed_to,
        "reprepared": reprepared,
        "comfortable": comfortable,
        **attrs,
    }
    with Span.open(SPAN_WWN_LONG_REST, attributes, tracer_override=_tracer):
        pass
