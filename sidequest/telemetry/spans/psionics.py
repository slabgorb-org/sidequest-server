"""Psionics + ruleset-namespaced Effort spans (Story 102-6).

The Effort economy is shared SWN-family crunch (lifted to ``SwnRulesetModule``),
so its commit/reclaim spans are namespaced by the resolved ruleset slug —
``{ruleset}.effort.commit`` / ``{ruleset}.effort.reclaim`` — per the story's
span contract. The ``wwn.effort.*`` routes are registered in ``spans/wwn.py``
(backward compat); this module adds the ``swn.effort.*`` twins plus the
discipline-activation span for both slugs.

The GM panel is the lie detector: a psionic activation that claims to have
committed Effort or pushed Strain must leave a span, or it's improvisation.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# ---------------------------------------------------------------------------
# {ruleset}.effort.commit / {ruleset}.effort.reclaim — swn twins of the wwn
# routes (wwn.effort.* registered in spans/wwn.py). The slug-substituted name
# is computed at emit time; both concrete names carry a routing decision.
# ---------------------------------------------------------------------------

SPAN_SWN_EFFORT_COMMIT = "swn.effort.commit"
SPAN_ROUTES[SPAN_SWN_EFFORT_COMMIT] = SpanRoute(
    event_type="state_transition",
    component="swn",
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

SPAN_SWN_EFFORT_RECLAIM = "swn.effort.reclaim"
SPAN_ROUTES[SPAN_SWN_EFFORT_RECLAIM] = SpanRoute(
    event_type="state_transition",
    component="swn",
    extract=lambda span: {
        "field": "effort_reclaim",
        "actor": (span.attributes or {}).get("actor", ""),
        "source": (span.attributes or {}).get("source", ""),
        "points": (span.attributes or {}).get("points", 0),
        "trigger": (span.attributes or {}).get("trigger", ""),
        "available": (span.attributes or {}).get("available", 0),
    },
)

# ---------------------------------------------------------------------------
# {ruleset}.discipline.activated — psionic activation, shape per wwn.spell.cast.
# Registered for both slugs (swn psychics + wwn psionic traditions).
# ---------------------------------------------------------------------------


def _discipline_activated_extract(span: Any) -> dict[str, Any]:
    return {
        "field": "discipline_activated",
        "actor": (span.attributes or {}).get("actor", ""),
        "discipline_id": (span.attributes or {}).get("discipline_id", ""),
        "refused": (span.attributes or {}).get("refused", False),
    }


SPAN_SWN_DISCIPLINE_ACTIVATED = "swn.discipline.activated"
SPAN_ROUTES[SPAN_SWN_DISCIPLINE_ACTIVATED] = SpanRoute(
    event_type="state_transition",
    component="swn",
    extract=_discipline_activated_extract,
)

SPAN_WWN_DISCIPLINE_ACTIVATED = "wwn.discipline.activated"
SPAN_ROUTES[SPAN_WWN_DISCIPLINE_ACTIVATED] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=_discipline_activated_extract,
)


def effort_commit_span(
    *,
    ruleset: str,
    actor: str,
    source: str,
    points: int,
    duration: str,
    available: int,
    applied: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a ``{ruleset}.effort.commit`` span (lie-detector for Effort commitment).

    Slug-namespaced: a swn-resolved module reads ``swn.effort.commit``; a wwn
    one reads ``wwn.effort.commit`` (the existing constant, backward compatible).
    """
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
    with Span.open(f"{ruleset}.effort.commit", attributes, tracer_override=_tracer):
        pass


def effort_reclaim_span(
    *,
    ruleset: str,
    actor: str,
    source: str,
    points: int,
    trigger: str,
    available: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a ``{ruleset}.effort.reclaim`` span (lie-detector for Effort reclamation)."""
    attributes: dict[str, Any] = {
        "field": "effort_reclaim",
        "actor": actor,
        "source": source,
        "points": points,
        "trigger": trigger,
        "available": available,
        **attrs,
    }
    with Span.open(f"{ruleset}.effort.reclaim", attributes, tracer_override=_tracer):
        pass


def discipline_activated_span(
    *,
    ruleset: str,
    actor: str,
    discipline_id: str,
    refused: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a ``{ruleset}.discipline.activated`` span (lie-detector for psionics).

    Fired on EVERY activation — applied AND refused (``refused=True``) — so a
    no-free-Effort refusal surfaces loudly on the GM panel rather than as a
    silent success."""
    attributes: dict[str, Any] = {
        "field": "discipline_activated",
        "actor": actor,
        "discipline_id": discipline_id,
        "refused": refused,
        **attrs,
    }
    with Span.open(f"{ruleset}.discipline.activated", attributes, tracer_override=_tracer):
        pass
