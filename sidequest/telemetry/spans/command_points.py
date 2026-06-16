"""command_points.* OTEL spans for the War Rig command economy (Story 86-7).

The SWN §4.3 Command Points layer on the crewed War Rig (86-6). Two routed
``state_transition`` emitters (component="command_points") so the GM dashboard's
Subsystems tab renders the command economy as typed events — the lie-detector
mandate (CLAUDE.md OTEL principle): a narrator claiming "you push the engine past
the red line" with no ``command_points.*`` span is improvising.

  - ``command_points.action_taken`` — fires for EVERY CP action (including the
    free Do Your Duty), recording which seat spent what on the shared pool.
  - ``command_points.delta`` — fires only when the pool actually changes (cost>0),
    mirroring ``rig_pool.delta``'s realized-change contract.

Routed per the ``rig_pool.*`` precedent (telemetry/spans/rig.py). Emitters fire on
the spend point and have no inner work — ``pass`` inside ``Span.open`` is intentional.
"""

from __future__ import annotations

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_CP_ACTION_TAKEN = "command_points.action_taken"
SPAN_ROUTES[SPAN_CP_ACTION_TAKEN] = SpanRoute(
    event_type="state_transition",
    component="command_points",
    extract=lambda span: {
        "field": "command_points",
        "op": "action_taken",
        "vessel_id": (span.attributes or {}).get("vessel_id", ""),
        "seat": (span.attributes or {}).get("seat", ""),
        "action": (span.attributes or {}).get("action", ""),
        "cost": (span.attributes or {}).get("cost", 0),
        "bonus": (span.attributes or {}).get("bonus", 0),
    },
)

SPAN_CP_DELTA = "command_points.delta"
SPAN_ROUTES[SPAN_CP_DELTA] = SpanRoute(
    event_type="state_transition",
    component="command_points",
    extract=lambda span: {
        "field": "command_points",
        "op": "delta",
        "vessel_id": (span.attributes or {}).get("vessel_id", ""),
        "seat": (span.attributes or {}).get("seat", ""),
        "action": (span.attributes or {}).get("action", ""),
        "delta": (span.attributes or {}).get("delta", 0),
        "old_current": (span.attributes or {}).get("old_current", 0),
        "new_current": (span.attributes or {}).get("new_current", 0),
    },
)


def emit_command_points_action_taken(
    *, vessel_id: str, seat: str, action: str, cost: int, bonus: int
) -> None:
    with Span.open(
        SPAN_CP_ACTION_TAKEN,
        attrs={
            "vessel_id": vessel_id,
            "seat": seat,
            "action": action,
            "cost": cost,
            "bonus": bonus,
        },
    ):
        pass


def emit_command_points_delta(
    *, vessel_id: str, seat: str, action: str, delta: int, old_current: int, new_current: int
) -> None:
    with Span.open(
        SPAN_CP_DELTA,
        attrs={
            "vessel_id": vessel_id,
            "seat": seat,
            "action": action,
            "delta": delta,
            "old_current": old_current,
            "new_current": new_current,
        },
    ):
        pass


__all__ = [
    "SPAN_CP_ACTION_TAKEN",
    "SPAN_CP_DELTA",
    "emit_command_points_action_taken",
    "emit_command_points_delta",
]
