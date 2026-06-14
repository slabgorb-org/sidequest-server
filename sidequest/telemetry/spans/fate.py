"""Fate ruleset OTEL spans (ADR-144). The GM panel is the lie detector: a Fate
roll that fired emits ``fate.action_resolved`` carrying the full math."""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

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


__all__ = ["fate_action_resolved_span"]
