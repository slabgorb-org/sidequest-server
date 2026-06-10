"""WN-family resolution spans — slug-parametrized (Story 102-5, ADR-117).

One narrator tool contract (``wn_attack`` / ``wn_skill_check`` / ``wn_save`` /
``wn_adjudicate_dead_premise``) spans the four "Without Number" modules
(swn/wwn/cwn/awn). The resolution span carries the BOUND module's slug so the
GM panel reads honest provenance: a wwn pack emits ``wwn.attack.resolved``, an
awn pack ``awn.attack.resolved`` — the slug-honesty invariant the epic pins.

Span names are dynamic (``{slug}.{event}``), so this module registers a route
per (slug, event) pair rather than per-name ``SPAN_*`` constants. The
routing-completeness lint scans ``SPAN_*`` module attributes; the dynamic names
are routed here without minting one constant apiece. Every route emits a
``state_transition`` typed event so the dashboard's Subsystems feed sees the
mechanical decision (the lie detector — CLAUDE.md OTEL Observability Principle).
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute, _SpanLike
from .span import Span

# The four "Without Number" module slugs the WN tool contract serves.
WN_FAMILY_SLUGS: tuple[str, ...] = ("swn", "wwn", "cwn", "awn")

# Event suffix -> the ``state_transition`` field label the GM panel groups on.
_WN_EVENTS: dict[str, str] = {
    "attack.resolved": "wn_attack",
    "skill_check.resolved": "wn_skill_check",
    "save.resolved": "wn_save",
    "dead_premise.adjudicated": "wn_dead_premise",
}


def _make_extract(field: str):
    def _extract(span: _SpanLike) -> dict[str, Any]:
        attrs = dict(span.attributes or {})
        attrs["field"] = field
        return attrs

    return _extract


for _slug in WN_FAMILY_SLUGS:
    for _event, _field in _WN_EVENTS.items():
        SPAN_ROUTES[f"{_slug}.{_event}"] = SpanRoute(
            event_type="state_transition",
            component=_slug,
            extract=_make_extract(_field),
        )


def wn_attack_resolved_span(
    *,
    slug: str,
    actor: str,
    target: str,
    weapon: str,
    hit: bool,
    d20: int,
    modifier: int,
    attack_total: int,
    target_ac: int,
    damage: int,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Emit ``{slug}.attack.resolved`` — the GM-panel record of a WN attack roll."""
    with Span.open(
        f"{slug}.attack.resolved",
        {
            "actor": actor,
            "target": target,
            "weapon": weapon,
            "hit": hit,
            "d20": d20,
            "modifier": modifier,
            "attack_total": attack_total,
            "target_ac": target_ac,
            "damage": damage,
        },
        tracer_override=_tracer,
    ):
        pass


def wn_skill_check_resolved_span(
    *,
    slug: str,
    actor: str,
    skill: str,
    attribute: str,
    difficulty: int,
    total: int,
    modifier: int,
    success: bool,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Emit ``{slug}.skill_check.resolved`` — the GM-panel record of a 2d6 check."""
    with Span.open(
        f"{slug}.skill_check.resolved",
        {
            "actor": actor,
            "skill": skill,
            "attribute": attribute,
            "difficulty": difficulty,
            "total": total,
            "modifier": modifier,
            "success": success,
        },
        tracer_override=_tracer,
    ):
        pass


def wn_save_resolved_span(
    *,
    slug: str,
    actor: str,
    save: str,
    effect: str,
    target: int,
    d20: int,
    modifier: int,
    success: bool,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Emit ``{slug}.save.resolved`` — the GM-panel record of a d20 save."""
    with Span.open(
        f"{slug}.save.resolved",
        {
            "actor": actor,
            "save": save,
            "effect": effect,
            "target": target,
            "d20": d20,
            "modifier": modifier,
            "success": success,
        },
        tracer_override=_tracer,
    ):
        pass


def wn_dead_premise_adjudicated_span(
    *,
    slug: str,
    actor: str,
    action: str,
    gone_target: str,
    ruling: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Emit ``{slug}.dead_premise.adjudicated`` — records redirect-vs-fizzle AND why."""
    with Span.open(
        f"{slug}.dead_premise.adjudicated",
        {
            "actor": actor,
            "action": action,
            "gone_target": gone_target,
            "ruling": ruling,
            "reason": reason,
        },
        tracer_override=_tracer,
    ):
        pass
