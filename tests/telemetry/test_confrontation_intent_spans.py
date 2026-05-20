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


def test_confrontation_intent_mismatch_resolved_span() -> None:
    from sidequest.telemetry.spans import confrontation_intent_mismatch_resolved_span

    tracer, exporter = _fresh_tracer_and_exporter()

    with confrontation_intent_mismatch_resolved_span(matched_type="combat", _tracer=tracer):
        pass

    spans = exporter.get_finished_spans()
    assert any(s.name == "confrontation.intent_mismatch_resolved" for s in spans)
    matching = [s for s in spans if s.name == "confrontation.intent_mismatch_resolved"]
    assert dict(matching[0].attributes or {})["matched_type"] == "combat"


def test_confrontation_intent_mismatch_reprompt_failed_span() -> None:
    from sidequest.telemetry.spans import confrontation_intent_mismatch_reprompt_failed_span

    tracer, exporter = _fresh_tracer_and_exporter()

    with confrontation_intent_mismatch_reprompt_failed_span(matched_type="combat", _tracer=tracer):
        pass

    spans = exporter.get_finished_spans()
    assert any(s.name == "confrontation.intent_mismatch_reprompt_failed" for s in spans)


def test_span_constants_routed() -> None:
    """Routing-completeness check — every new SPAN_* either routed or flat-only."""
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_CONFRONTATION_INTENT_MISMATCH,
        SPAN_CONFRONTATION_INTENT_MISMATCH_REPROMPT_FAILED,
        SPAN_CONFRONTATION_INTENT_MISMATCH_RESOLVED,
        SPAN_ROUTES,
    )
    for name in (
        SPAN_CONFRONTATION_INTENT_MISMATCH,
        SPAN_CONFRONTATION_INTENT_MISMATCH_RESOLVED,
        SPAN_CONFRONTATION_INTENT_MISMATCH_REPROMPT_FAILED,
    ):
        assert name in SPAN_ROUTES or name in FLAT_ONLY_SPANS, (
            f"span {name} is neither routed nor flat-only"
        )
