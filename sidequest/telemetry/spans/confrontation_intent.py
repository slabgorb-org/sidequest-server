"""Confrontation intent-validator spans.

Spec 2026-05-20 confrontation-intent-validator step 8. Fired by the
validator dispatch in sidequest.server.narration_apply and the reprompt
loop in sidequest.server.websocket_session_handler._execute_narration_turn.

The intent_mismatch span is the ADR-067 inference-site emission promised
in the unified-narrator-agent design. Replaces the legacy
state_transition field=confrontation op=skipped_with_trigger_keywords
watcher event (deleted in Task 9).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_CONFRONTATION_INTENT_MISMATCH = "confrontation.intent_mismatch"
SPAN_ROUTES[SPAN_CONFRONTATION_INTENT_MISMATCH] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.intent_mismatch",
        "matched_type": (span.attributes or {}).get("matched_type", ""),
        "declared_type": (span.attributes or {}).get("declared_type", ""),
        "severity": (span.attributes or {}).get("severity", ""),
        "matched_tokens": (span.attributes or {}).get("matched_tokens", ""),
        "reprompt_attempted": (span.attributes or {}).get("reprompt_attempted", False),
        "outcome": (span.attributes or {}).get("outcome", ""),
    },
)

SPAN_CONFRONTATION_INTENT_MISMATCH_RESOLVED = "confrontation.intent_mismatch_resolved"
SPAN_ROUTES[SPAN_CONFRONTATION_INTENT_MISMATCH_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.intent_mismatch_resolved",
        "matched_type": (span.attributes or {}).get("matched_type", ""),
    },
)

SPAN_CONFRONTATION_INTENT_MISMATCH_REPROMPT_FAILED = (
    "confrontation.intent_mismatch_reprompt_failed"
)
SPAN_ROUTES[SPAN_CONFRONTATION_INTENT_MISMATCH_REPROMPT_FAILED] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.intent_mismatch_reprompt_failed",
        "matched_type": (span.attributes or {}).get("matched_type", ""),
    },
)


@contextmanager
def confrontation_intent_mismatch_span(
    *,
    matched_type: str,
    declared_type: str | None,
    severity: str,
    matched_tokens: tuple[str, ...],
    reprompt_attempted: bool = False,
    outcome: str | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emitted when the validator returns a ValidationResult."""
    span_attrs: dict[str, Any] = {
        "matched_type": matched_type,
        "declared_type": declared_type or "",
        "severity": severity,
        "matched_tokens": ",".join(matched_tokens),
        "reprompt_attempted": reprompt_attempted,
    }
    if outcome is not None:
        span_attrs["outcome"] = outcome
    span_attrs.update(attrs)
    with Span.open(
        SPAN_CONFRONTATION_INTENT_MISMATCH,
        span_attrs,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def confrontation_intent_mismatch_resolved_span(
    *,
    matched_type: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emitted when the reprompt loop's second call resolves the mismatch."""
    span_attrs = {"matched_type": matched_type, **attrs}
    with Span.open(
        SPAN_CONFRONTATION_INTENT_MISMATCH_RESOLVED,
        span_attrs,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def confrontation_intent_mismatch_reprompt_failed_span(
    *,
    matched_type: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emitted when the second narrator call raises during reprompt."""
    span_attrs = {"matched_type": matched_type, **attrs}
    with Span.open(
        SPAN_CONFRONTATION_INTENT_MISMATCH_REPROMPT_FAILED,
        span_attrs,
        tracer_override=_tracer,
    ) as span:
        yield span
