"""Dogfight sealed-letter resolution spans.

ADR-077 prescribes 7 spans. Three live below; the four deferred
(gun_solution_fired, energy_depleted, skill_tier_resolved, ace_instinct_used)
need subsystems that don't exist yet.
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
