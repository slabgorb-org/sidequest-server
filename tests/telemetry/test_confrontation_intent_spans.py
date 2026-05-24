"""OTEL span tests for confrontation.intent_mismatch family.

Spec 2026-05-20 confrontation-intent-validator step 8.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    """Create an isolated in-memory exporter + tracer pair per test.

    Each call creates a fresh TracerProvider with its own InMemorySpanExporter
    and extracts a tracer from it. Spans are emitted via ``tracer_override``
    so they bypass the global provider entirely — no cross-test contamination
    and no issues with ``set_tracer_provider`` being rejected after the first
    call.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return tracer, exporter


def test_confrontation_intent_mismatch_span_attributes() -> None:
    from sidequest.telemetry.spans import confrontation_intent_mismatch_span

    tracer, exporter = _fresh_tracer_and_exporter()

    with confrontation_intent_mismatch_span(
        matched_type="negotiation",
        declared_type=None,
        severity="warn",
        matched_tokens=("bargain", "haggle"),
        reprompt_attempted=False,
        _tracer=tracer,
    ):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "confrontation.intent_mismatch"
    attrs = dict(spans[0].attributes or {})
    assert attrs["matched_type"] == "negotiation"
    assert attrs["declared_type"] == ""  # None → empty string
    assert attrs["severity"] == "warn"
    assert attrs["reprompt_attempted"] is False
    # matched_tokens is a comma-joined string for OTEL transport
    assert "bargain" in attrs["matched_tokens"]
    assert "haggle" in attrs["matched_tokens"]


def test_confrontation_intent_mismatch_span_with_outcome_label() -> None:
    from sidequest.telemetry.spans import confrontation_intent_mismatch_span

    tracer, exporter = _fresh_tracer_and_exporter()

    with confrontation_intent_mismatch_span(
        matched_type="combat",
        declared_type="negotiation",
        severity="warn",
        matched_tokens=("strike",),
        reprompt_attempted=True,
        outcome="fall_through",
        _tracer=tracer,
    ):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["outcome"] == "fall_through"
    assert attrs["declared_type"] == "negotiation"


# Story 59-3 / ADR-113 retired the reprompt-failed and reprompt-resolved
# spans alongside the reprompt loop in _execute_narration_turn. The
# router-driven sidequest.agents.dispatch_engagement_watcher (covered by
# tests/agents/test_dispatch_engagement_watcher.py +
# tests/telemetry/test_dispatch_engagement_spans.py) replaces both —
# "one mechanism per problem" (memory feedback_one_mechanism_per_problem).


def test_span_constants_routed() -> None:
    """Routing-completeness check — every surviving SPAN_* either routed or flat-only."""
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_CONFRONTATION_INTENT_MISMATCH,
        SPAN_ROUTES,
    )

    assert (
        SPAN_CONFRONTATION_INTENT_MISMATCH in SPAN_ROUTES
        or SPAN_CONFRONTATION_INTENT_MISMATCH in FLAT_ONLY_SPANS
    ), f"span {SPAN_CONFRONTATION_INTENT_MISMATCH} is neither routed nor flat-only"
