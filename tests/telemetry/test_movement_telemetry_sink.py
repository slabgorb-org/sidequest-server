"""Movement spans must mirror into the turn_telemetry DB sink, not just Jaeger.

Root cause behind 8 failed crossing attempts (2026-06-22 findings): movement
reaches Jaeger via Span.open but never turn_telemetry, so a firing engine reads
as DEAD in saves. Every movement emit site flows through these 3 context
managers, so mirroring there covers the subsystem, narration_apply, AND both
seam resolvers (deep_descent = the real ropefoot->entrance crossing).
"""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
import sidequest.telemetry.spans.movement as mv


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-movement-telemetry-sink")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def test_resolved_span_mirrors_to_sink(monkeypatch, capture_spans):
    published: list[tuple] = []
    monkeypatch.setattr(
        mv, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )

    with mv.movement_resolved_span(
        pc_name="Groucho", from_region="ropefoot", to_region="entrance"
    ) as span:
        span.set_attribute("resolved_via", "surface_descent_adjacent")
        span.set_attribute("edge_kind", "surface_descent")

    assert published, "movement.resolved did not mirror into the turn_telemetry sink"
    event_type, fields, kw = published[0]
    assert event_type == "state_transition"
    assert kw.get("component") == "movement"
    assert fields["op"] == "movement.resolved"
    assert fields["from_region"] == "ropefoot"
    assert fields["to_region"] == "entrance"
    assert fields["resolved_via"] == "surface_descent_adjacent"


def test_unresolved_span_mirrors_to_sink(monkeypatch, capture_spans):
    published: list[tuple] = []
    monkeypatch.setattr(
        mv, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )

    with mv.movement_unresolved_span(
        pc_name="Groucho", reason="no_candidate_edges", from_region="exp002.r2"
    ) as span:
        span.set_attribute("available_exits", [])

    assert published, "movement.unresolved did not mirror into the sink"
    _, fields, kw = published[0]
    assert kw.get("component") == "movement"
    assert fields["op"] == "movement.unresolved"
    assert fields["reason"] == "no_candidate_edges"


def test_region_mode_span_mirrors_to_sink(monkeypatch, capture_spans):
    published: list[tuple] = []
    monkeypatch.setattr(
        mv, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )

    with mv.movement_region_mode_span(pc_name="Dorothy", from_region="munchkin_country") as span:
        span.set_attribute("world_slug", "oz")

    assert published, "movement.region_mode did not mirror into the sink"
    _, fields, kw = published[0]
    assert kw.get("component") == "movement"
    assert fields["op"] == "movement.region_mode"
    assert fields["from_region"] == "munchkin_country"
