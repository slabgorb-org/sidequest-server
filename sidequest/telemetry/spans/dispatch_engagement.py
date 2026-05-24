"""Dispatch-engagement mismatch spans — Story 59-3 / ADR-113.

Fired by the post-turn watcher in
:mod:`sidequest.agents.dispatch_engagement_watcher` when the Intent Router
dispatched a subsystem but the engine did not engage on the post-turn
snapshot. The GM panel reads these spans as the lie-detector signal that
the narrator produced convincing prose with zero mechanical backing —
the SOUL "Illusionism" failure mode the Intent Router spine exists to
prevent.

One span name per subsystem in the dispatch vocabulary so the panel can
filter by subsystem without parsing the name; ``subsystem`` is also
surfaced as a span attribute so route consumers can branch on it cheaply.

Replaces the 59-1 ``confrontation.intent_mismatch_reprompt_failed`` self-
report-reprompt span — one mechanism per problem
(memory ``feedback_one_mechanism_per_problem``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH = "dispatch_engagement.confrontation.mismatch"
SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH = "dispatch_engagement.magic_working.mismatch"
SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH = "dispatch_engagement.scenario_clue.mismatch"


def _extract(span: Any) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "dispatch_engagement.mismatch",
        "subsystem": attrs.get("subsystem", ""),
        "idempotency_key": attrs.get("idempotency_key", ""),
        "dispatched_type": attrs.get("dispatched_type", ""),
        "evidence": attrs.get("evidence", ""),
    }


for _name in (
    SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH,
    SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH,
    SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH,
):
    SPAN_ROUTES[_name] = SpanRoute(
        event_type="state_transition",
        component="intent_router",
        extract=_extract,
    )


_SUBSYSTEM_TO_SPAN_NAME: dict[str, str] = {
    "confrontation": SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH,
    "magic_working": SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH,
    "scenario_clue": SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH,
}


def span_name_for_subsystem(subsystem: str) -> str:
    """Map a dispatched subsystem name to its mismatch span name.

    Raises :class:`KeyError` if ``subsystem`` is not in the dispatch
    vocabulary — fail loud (memory ``feedback_no_fallbacks_hard``) so a
    typo in the router's dispatch table surfaces immediately rather than
    silently routing to an "unknown subsystem" span the GM panel cannot
    render.
    """
    return _SUBSYSTEM_TO_SPAN_NAME[subsystem]


@contextmanager
def dispatch_engagement_mismatch_span(
    *,
    subsystem: str,
    idempotency_key: str = "",
    dispatched_type: str = "",
    evidence: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emit a ``dispatch_engagement.{subsystem}.mismatch`` span.

    The watcher emits one of these per detected mismatch — one turn with
    three failed dispatches produces three distinct spans, not one
    aggregated event. The GM panel needs per-subsystem accountability.
    """
    name = span_name_for_subsystem(subsystem)
    span_attrs: dict[str, Any] = {
        "subsystem": subsystem,
        "idempotency_key": idempotency_key,
        "dispatched_type": dispatched_type,
        "evidence": evidence,
    }
    span_attrs.update(attrs)
    with Span.open(name, span_attrs, tracer_override=_tracer) as span:
        yield span


__all__ = [
    "SPAN_DISPATCH_ENGAGEMENT_CONFRONTATION_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_MAGIC_WORKING_MISMATCH",
    "SPAN_DISPATCH_ENGAGEMENT_SCENARIO_CLUE_MISMATCH",
    "dispatch_engagement_mismatch_span",
    "span_name_for_subsystem",
]
