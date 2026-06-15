"""RED (Story 117-6, AC-2): the ``narration.unminted_objective.suspected`` span
must carry a ``detection_method`` so the GM panel (the lie-detector) can tell
WHICH path flagged the objective — the new classifier vs. the legacy keyword
backstop. Without it a reviewer cannot tell whether the un-seeded classifier
engaged or the brittle substring matcher merely got lucky.

The span exists (wired in 117-4) and is routed to component="narrator". 117-6
adds the ``detection_method`` attribute and surfaces it through the SPAN_ROUTES
extract so it reaches the panel feed.

Today ``narration_unminted_objective_span`` takes only ``evidence`` and the
extract surfaces only ``evidence`` — so the ``detection_method`` assertions below
FAIL (unexpected-keyword / missing key) until 117-6 threads the method through.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_span_accepts_and_records_classifier_detection_method() -> None:
    """The classifier path tags the span detection_method='classifier'."""
    from sidequest.telemetry.spans.dispatch_engagement import (
        narration_unminted_objective_span,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    with narration_unminted_objective_span(
        evidence="floor-boss handed a discreet job; quest_log empty",
        detection_method="classifier",
        _tracer=tracer,
    ):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("detection_method") == "classifier", (
        f"span must record detection_method='classifier'; got {attrs}"
    )
    # The evidence attribute is preserved alongside the new field.
    assert "quest_log empty" in attrs.get("evidence", "")


def test_span_defaults_detection_method_to_keyword_for_legacy_path() -> None:
    """The legacy keyword backstop carries detection_method='keyword' so the GM
    panel can distinguish a real classification from a brittle substring hit. The
    span's default (no explicit method) represents the legacy keyword path."""
    from sidequest.telemetry.spans.dispatch_engagement import (
        narration_unminted_objective_span,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    with narration_unminted_objective_span(
        evidence="curated marker fired; quest_log empty",
        _tracer=tracer,
    ):
        pass

    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attrs.get("detection_method") == "keyword", (
        f"the legacy keyword backstop must tag detection_method='keyword'; got {attrs}"
    )


def test_span_route_extract_surfaces_detection_method() -> None:
    """The SPAN_ROUTES extract for the span must include detection_method so the
    GM-panel feed carries it through to the narrator-attributed event — not just
    the raw OTEL attribute."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    name = "narration.unminted_objective.suspected"
    assert name in SPAN_ROUTES, f"{name} not registered in SPAN_ROUTES"
    route = SPAN_ROUTES[name]
    assert route.component == "narrator"

    class _FakeSpan:
        attributes = {
            "evidence": "floor-boss handed a discreet job",
            "detection_method": "classifier",
        }

    extracted = route.extract(_FakeSpan())
    assert extracted.get("detection_method") == "classifier", (
        f"the span route extract must surface detection_method; got {extracted}"
    )
