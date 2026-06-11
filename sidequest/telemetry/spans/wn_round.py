"""WN sealed-round OTEL spans (story 102-4). GM panel = lie detector.

The WN turn model (sealed commitment → initiative-ordered resolution) is
family behavior shared by all four sisters, so the span names are
slug-parametrized per the epic's ``{ruleset}.{surface}`` invariant — a pack
bound to ``awn`` emits ``awn.round.resolved``, never ``cwn.*`` (honest slug
per binding). Routes are registered for every family slug at import time so
the watcher translator can type each one.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# The WN family binding slugs (registry keys). WWN/CWN subclass SWN and AWN
# subclasses CWN, so one turn-model implementation serves all four — but each
# binding keeps its own honest span namespace.
WN_FAMILY_SLUGS = ("swn", "wwn", "cwn", "awn")

for _slug in WN_FAMILY_SLUGS:
    SPAN_ROUTES[f"{_slug}.round.committed"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "round_committed",
            "committed_actors": (span.attributes or {}).get("committed_actors", ""),
            "exempt_allies": (span.attributes or {}).get("exempt_allies", ""),
        },
    )
    SPAN_ROUTES[f"{_slug}.round.initiative"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "round_initiative",
            "initiative_order": (span.attributes or {}).get("initiative_order", ""),
        },
    )
    SPAN_ROUTES[f"{_slug}.round.resolved"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "round_resolved",
            "resolution_order": (span.attributes or {}).get("resolution_order", ""),
        },
    )
    SPAN_ROUTES[f"{_slug}.dead_premise"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "dead_premise",
            "actor": (span.attributes or {}).get("actor", ""),
            "target": (span.attributes or {}).get("target", ""),
        },
    )


def _require_family_slug(slug: str) -> None:
    if slug not in WN_FAMILY_SLUGS:
        raise ValueError(
            f"WN round spans are family-only; got slug {slug!r} "
            f"(expected one of {WN_FAMILY_SLUGS}) — No Silent Fallbacks"
        )


def wn_round_committed_span(
    *,
    slug: str,
    committed_actors: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.round.committed — the sealed-commit barrier closed."""
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "round_committed",
        "committed_actors": committed_actors,
        **attrs,
    }
    with Span.open(f"{slug}.round.committed", attributes, tracer_override=_tracer):
        pass


def wn_round_initiative_span(
    *,
    slug: str,
    initiative_order: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.round.initiative — the persisted order this round resolves in."""
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "round_initiative",
        "initiative_order": initiative_order,
        **attrs,
    }
    with Span.open(f"{slug}.round.initiative", attributes, tracer_override=_tracer):
        pass


def wn_round_resolved_span(
    *,
    slug: str,
    resolution_order: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.round.resolved — the round walk finished.

    ``resolution_order`` is the comma-space-joined token_id sequence the
    engine actually walked (descending persisted initiative) — the
    lie-detector for "whose shot lands first".
    """
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "round_resolved",
        "resolution_order": resolution_order,
        **attrs,
    }
    with Span.open(f"{slug}.round.resolved", attributes, tracer_override=_tracer):
        pass


def wn_dead_premise_span(
    *,
    slug: str,
    actor: str,
    target: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.dead_premise — a committed action's target was already down.

    The engine surfaces the event and does NOT resolve the action; the
    narrator adjudicates redirect-or-fizzle in fiction (SOUL: The Test).
    """
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "dead_premise",
        "actor": actor,
        "target": target,
        **attrs,
    }
    with Span.open(f"{slug}.dead_premise", attributes, tracer_override=_tracer):
        pass
