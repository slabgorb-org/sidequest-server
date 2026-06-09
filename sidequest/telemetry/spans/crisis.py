"""crisis.* OTEL spans for the War Rig d10 Crisis table (Story 86-7).

The SWN §4.3.4 Crisis layer on the crewed War Rig. Three routed
``state_transition`` emitters (component="crisis") so the GM dashboard can show the
active threat, whether the crew answered it, and — for a continuing crisis they
failed — that it escalated and bit the Hull.

  - ``crisis.rolled``    — a crisis arises (d10 → table entry).
  - ``crisis.resolved``  — a Deal With a Crisis attempt resolves (success flag).
  - ``crisis.escalated`` — a FAILED continuing crisis worsens; ``hull_delta`` reports
    the penalty applied to the 86-2 two-pool Hull when one is supplied (which fires its
    own rig_pool.delta). The ``war_rig_crew`` round supplies the crew's shared Hull, so
    this damages it in live play; ``hull_delta`` is 0 (no rig_pool.delta) if no Hull.

Routed per the ``rig_pool.*`` precedent (telemetry/spans/rig.py). Emitters fire on
the crisis decision point and have no inner work — ``pass`` is intentional.
"""

from __future__ import annotations

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_CRISIS_ROLLED = "crisis.rolled"
SPAN_ROUTES[SPAN_CRISIS_ROLLED] = SpanRoute(
    event_type="state_transition",
    component="crisis",
    extract=lambda span: {
        "field": "crisis",
        "op": "rolled",
        "vessel_id": (span.attributes or {}).get("vessel_id", ""),
        "roll": (span.attributes or {}).get("roll", 0),
        "crisis_id": (span.attributes or {}).get("crisis_id", ""),
        "crisis_type": (span.attributes or {}).get("crisis_type", ""),
        "dc": (span.attributes or {}).get("dc", 0),
    },
)

SPAN_CRISIS_RESOLVED = "crisis.resolved"
SPAN_ROUTES[SPAN_CRISIS_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="crisis",
    extract=lambda span: {
        "field": "crisis",
        "op": "resolved",
        "vessel_id": (span.attributes or {}).get("vessel_id", ""),
        "seat": (span.attributes or {}).get("seat", ""),
        "crisis_id": (span.attributes or {}).get("crisis_id", ""),
        "roll": (span.attributes or {}).get("roll", 0),
        "total": (span.attributes or {}).get("total", 0),
        "success": (span.attributes or {}).get("success", False),
    },
)

SPAN_CRISIS_ESCALATED = "crisis.escalated"
SPAN_ROUTES[SPAN_CRISIS_ESCALATED] = SpanRoute(
    event_type="state_transition",
    component="crisis",
    extract=lambda span: {
        "field": "crisis",
        "op": "escalated",
        "vessel_id": (span.attributes or {}).get("vessel_id", ""),
        "seat": (span.attributes or {}).get("seat", ""),
        "crisis_id": (span.attributes or {}).get("crisis_id", ""),
        "hull_delta": (span.attributes or {}).get("hull_delta", 0),
    },
)


def emit_crisis_rolled(
    *, vessel_id: str, roll: int, crisis_id: str, crisis_type: str, dc: int
) -> None:
    with Span.open(
        SPAN_CRISIS_ROLLED,
        attrs={
            "vessel_id": vessel_id,
            "roll": roll,
            "crisis_id": crisis_id,
            "crisis_type": crisis_type,
            "dc": dc,
        },
    ):
        pass


def emit_crisis_resolved(
    *, vessel_id: str, seat: str, crisis_id: str, roll: int, total: int, success: bool
) -> None:
    with Span.open(
        SPAN_CRISIS_RESOLVED,
        attrs={
            "vessel_id": vessel_id,
            "seat": seat,
            "crisis_id": crisis_id,
            "roll": roll,
            "total": total,
            "success": success,
        },
    ):
        pass


def emit_crisis_escalated(*, vessel_id: str, seat: str, crisis_id: str, hull_delta: int) -> None:
    with Span.open(
        SPAN_CRISIS_ESCALATED,
        attrs={
            "vessel_id": vessel_id,
            "seat": seat,
            "crisis_id": crisis_id,
            "hull_delta": hull_delta,
        },
    ):
        pass


__all__ = [
    "SPAN_CRISIS_ESCALATED",
    "SPAN_CRISIS_RESOLVED",
    "SPAN_CRISIS_ROLLED",
    "emit_crisis_escalated",
    "emit_crisis_resolved",
    "emit_crisis_rolled",
]
