"""Dogfight sealed-letter resolution spans.

ADR-077 (per the dogfight × SWN resolution work). Five spans live below:
confrontation_started, maneuver_committed, cell_resolved, plus shot_attempted
(hit resolution) and shot_damage (damage ablation). Four remain deferred —
gun_solution_fired, energy_depleted, skill_tier_resolved, ace_instinct_used —
because they need subsystems that don't exist yet. The two SWN shot spans are
additions to the live set; the deferred list is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_DOGFIGHT_CONFRONTATION_STARTED = "dogfight.confrontation_started"
SPAN_ROUTES[SPAN_DOGFIGHT_CONFRONTATION_STARTED] = SpanRoute(
    event_type="state_transition",
    component="dogfight",
    extract=lambda span: {
        "field": "dogfight",
        "op": "confrontation_started",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "red_actor": (span.attributes or {}).get("red_actor", ""),
        "blue_actor": (span.attributes or {}).get("blue_actor", ""),
    },
)
SPAN_DOGFIGHT_MANEUVER_COMMITTED = "dogfight.maneuver_committed"
SPAN_ROUTES[SPAN_DOGFIGHT_MANEUVER_COMMITTED] = SpanRoute(
    event_type="state_transition",
    component="dogfight",
    extract=lambda span: {
        "field": "dogfight",
        "op": "maneuver_committed",
        "actor": (span.attributes or {}).get("actor", ""),
        "maneuver": (span.attributes or {}).get("maneuver", ""),
        "role": (span.attributes or {}).get("role", ""),
    },
)
SPAN_DOGFIGHT_CELL_RESOLVED = "dogfight.cell_resolved"
SPAN_ROUTES[SPAN_DOGFIGHT_CELL_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="dogfight",
    extract=lambda span: {
        "field": "dogfight",
        "op": "cell_resolved",
        "cell_name": (span.attributes or {}).get("cell_name", ""),
        "shape": (span.attributes or {}).get("shape", ""),
        "red_maneuver": (span.attributes or {}).get("red_maneuver", ""),
        "blue_maneuver": (span.attributes or {}).get("blue_maneuver", ""),
        "extend_and_return_triggered": (span.attributes or {}).get(
            "extend_and_return_triggered",
            False,
        ),
    },
)
SPAN_DOGFIGHT_SHOT_ATTEMPTED = "dogfight.shot_attempted"
SPAN_ROUTES[SPAN_DOGFIGHT_SHOT_ATTEMPTED] = SpanRoute(
    event_type="state_transition",
    component="dogfight",
    extract=lambda span: {
        "field": "dogfight",
        "op": "shot_attempted",
        "shooter": (span.attributes or {}).get("shooter", ""),
        "target": (span.attributes or {}).get("target", ""),
        "d20_total": (span.attributes or {}).get("d20_total", 0),
        "target_ac": (span.attributes or {}).get("target_ac", 0),
        "hit": (span.attributes or {}).get("hit", False),
        "geometry_modifier": (span.attributes or {}).get("geometry_modifier", 0),
        "source": (span.attributes or {}).get("source", ""),
    },
)
SPAN_DOGFIGHT_SHOT_DAMAGE = "dogfight.shot_damage"
SPAN_ROUTES[SPAN_DOGFIGHT_SHOT_DAMAGE] = SpanRoute(
    event_type="state_transition",
    component="dogfight",
    extract=lambda span: {
        "field": "dogfight",
        "op": "shot_damage",
        "shooter": (span.attributes or {}).get("shooter", ""),
        "target": (span.attributes or {}).get("target", ""),
        "dice": (span.attributes or {}).get("dice", ""),
        "armor_piercing": (span.attributes or {}).get("armor_piercing", 0),
        "armor_negated": (span.attributes or {}).get("armor_negated", 0),
        "applied": (span.attributes or {}).get("applied", 0),
        "target_hp_after": (span.attributes or {}).get("target_hp_after", 0),
    },
)
SPAN_DOGFIGHT_WEAPON_RESOLVED = "dogfight.weapon_resolved"
SPAN_ROUTES[SPAN_DOGFIGHT_WEAPON_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="dogfight",
    extract=lambda span: {
        "field": "dogfight",
        "op": "weapon_resolved",
        "source": (span.attributes or {}).get("source", ""),
        "weapon_id": (span.attributes or {}).get("weapon_id", ""),
        "armor_piercing": (span.attributes or {}).get("armor_piercing", 0),
        "dice": (span.attributes or {}).get("dice", ""),
    },
)


@contextmanager
def dogfight_confrontation_started_span(
    *,
    encounter_type: str,
    red_actor: str,
    blue_actor: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_DOGFIGHT_CONFRONTATION_STARTED,
        {
            "encounter_type": encounter_type,
            "red_actor": red_actor,
            "blue_actor": blue_actor,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dogfight_maneuver_committed_span(
    *,
    actor: str,
    maneuver: str,
    role: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_DOGFIGHT_MANEUVER_COMMITTED,
        {"actor": actor, "maneuver": maneuver, "role": role, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dogfight_cell_resolved_span(
    *,
    cell_name: str,
    shape: str,
    red_maneuver: str,
    blue_maneuver: str,
    extend_and_return_triggered: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_DOGFIGHT_CELL_RESOLVED,
        {
            "cell_name": cell_name,
            "shape": shape,
            "red_maneuver": red_maneuver,
            "blue_maneuver": blue_maneuver,
            "extend_and_return_triggered": extend_and_return_triggered,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dogfight_shot_attempted_span(
    *,
    shooter: str,
    target: str,
    d20_total: int,
    target_ac: int,
    hit: bool,
    geometry_modifier: int,
    source: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_DOGFIGHT_SHOT_ATTEMPTED,
        {
            "shooter": shooter,
            "target": target,
            "d20_total": d20_total,
            "target_ac": target_ac,
            "hit": hit,
            "geometry_modifier": geometry_modifier,
            "source": source,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dogfight_weapon_resolved_span(
    *,
    source: str,
    weapon_id: str,
    armor_piercing: int,
    dice: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """The dogfight resolved its ship weapon from ``source`` (story 114-15). The
    GM-panel lie-detector for "the dogfight used a real ship weapon with its
    armor_piercing" rather than Claude improvising one."""
    with Span.open(
        SPAN_DOGFIGHT_WEAPON_RESOLVED,
        {
            "source": source,
            "weapon_id": weapon_id,
            "armor_piercing": armor_piercing,
            "dice": dice,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dogfight_shot_damage_span(
    *,
    shooter: str,
    target: str,
    dice: str,
    armor_piercing: int,
    armor_negated: int,
    applied: int,
    target_hp_after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_DOGFIGHT_SHOT_DAMAGE,
        {
            "shooter": shooter,
            "target": target,
            "dice": dice,
            "armor_piercing": armor_piercing,
            "armor_negated": armor_negated,
            "applied": applied,
            "target_hp_after": target_hp_after,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


# ---------------------------------------------------------------------------
# Dispatch-level spans (Story 153-6 — [SWN-DOGFIGHT-UNREACHABLE]).
#
# The spans above fire from inside the dogfight ENGINE (seating + sealed-letter
# resolution). These two fire from the IntentRouter dispatch HANDLER
# (``agents/subsystems/dogfight.py``) — the GM-panel lie-detector confirming
# that a ship-combat intent ENGAGED the dogfight engine (``dogfight.dispatch``)
# rather than the narrator improvising it, and that an un-seatable dogfight
# failed LOUD (``dogfight.dispatch.rejected``) instead of silently handing back
# to the narrator (No Silent Fallbacks; AC-5). Mirrors the 153-5 ``course.plot``
# / ``course.plot.rejected`` handler-level pair.
# ---------------------------------------------------------------------------

SPAN_DOGFIGHT_DISPATCH = "dogfight.dispatch"
SPAN_ROUTES[SPAN_DOGFIGHT_DISPATCH] = SpanRoute(
    event_type="state_transition",
    component="dogfight",
    extract=lambda span: {
        "field": "dogfight",
        "op": "dispatch",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "opponent": (span.attributes or {}).get("opponent", ""),
    },
)
SPAN_DOGFIGHT_DISPATCH_REJECTED = "dogfight.dispatch.rejected"
SPAN_ROUTES[SPAN_DOGFIGHT_DISPATCH_REJECTED] = SpanRoute(
    event_type="state_transition",
    component="dogfight",
    extract=lambda span: {
        "field": "dogfight",
        "op": "dispatch_rejected",
        "reason": (span.attributes or {}).get("reason", ""),
        "opponent": (span.attributes or {}).get("opponent", ""),
    },
)


@contextmanager
def dogfight_dispatch_span(
    *,
    encounter_type: str,
    opponent: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """A ship-combat intent dispatched to the dogfight subsystem and SEATED a
    dogfight encounter (Story 153-6, AC-2). The GM-panel proof that the engine
    engaged, not that the narrator improvised ship combat."""
    with Span.open(
        SPAN_DOGFIGHT_DISPATCH,
        {"encounter_type": encounter_type, "opponent": opponent, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def dogfight_dispatch_rejected_span(
    *,
    reason: str,
    opponent: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """A dogfight dispatch could NOT seat the engine (no Other to seat, no
    dogfight ConfrontationDef, instantiation refused) — a LOUD failure with a
    reason the GM panel can read (Story 153-6, AC-5; No Silent Fallbacks). Never
    a silent hand-back to the narrator."""
    with Span.open(
        SPAN_DOGFIGHT_DISPATCH_REJECTED,
        {"reason": reason, "opponent": opponent, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span
