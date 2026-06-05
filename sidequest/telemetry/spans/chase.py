"""chase.* OTEL span — CWN §2.6.2 vehicle-chase pace/pursuit telemetry.

Story 86-3 (Plan 3, Epic 86 Road Warrior). One routed ``state_transition``
emitter (component="chase"): ``chase.pursuit_resolved``, fired once per
chase round by :func:`sidequest.game.chase_pace.resolve_chase_round` so the
GM dashboard's Subsystems tab can audit the §2.6.2 decision — pace vs. the
situational-adjusted pursuer roll, caught/evaded, and the convergence into
Plan 2 vehicle combat — instead of trusting improvised chase prose.

Mirrors the ``rig_pool.*`` routing (story 53-4): a typed event with
component="chase" rather than an opaque ``agent_span_close`` entry. Per the
magic.py precedent, a None location is coerced to "" so OTEL keeps it.
"""

from __future__ import annotations

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_CHASE_PURSUIT_RESOLVED = "chase.pursuit_resolved"
SPAN_ROUTES[SPAN_CHASE_PURSUIT_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="chase",
    extract=lambda span: {
        "field": "chase",
        "op": "pursuit_resolved",
        "pace": (span.attributes or {}).get("pace", 0),
        "pursuer_effective": (span.attributes or {}).get("pursuer_effective", 0),
        "outcome": (span.attributes or {}).get("outcome", ""),
        "converges_to_combat": (span.attributes or {}).get("converges_to_combat", False),
        "fleeing_id": (span.attributes or {}).get("fleeing_id", ""),
        "pursuer_id": (span.attributes or {}).get("pursuer_id", ""),
        "location": (span.attributes or {}).get("location", ""),
    },
)


def emit_chase_pursuit_resolved(
    *,
    pace: int,
    pursuer_effective: int,
    outcome: str,
    converges_to_combat: bool,
    fleeing_id: str,
    pursuer_id: str,
    location: str | None,
) -> None:
    """Emit one ``chase.pursuit_resolved`` span for a resolved chase round.

    The span carries the realized §2.6.2 decision (ADR-031 Layer-2) so the
    GM panel renders the round without re-deriving the math.
    """
    with Span.open(
        SPAN_CHASE_PURSUIT_RESOLVED,
        attrs={
            "pace": pace,
            "pursuer_effective": pursuer_effective,
            "outcome": outcome,
            "converges_to_combat": converges_to_combat,
            "fleeing_id": fleeing_id,
            "pursuer_id": pursuer_id,
            "location": location or "",
        },
    ):
        pass


__all__ = ["SPAN_CHASE_PURSUIT_RESOLVED", "emit_chase_pursuit_resolved"]
