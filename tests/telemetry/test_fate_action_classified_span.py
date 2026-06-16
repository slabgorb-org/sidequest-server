from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans._core import SPAN_ROUTES
from sidequest.telemetry.spans.fate import fate_action_classified_span


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_span_emits_with_attributes():
    exporter, tracer = _otel()
    fate_action_classified_span(
        actor="Hero",
        action="attack",
        skill="Fight",
        target="Thug",
        confidence=0.91,
        _tracer=tracer,
    )
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "fate.action.classified" in spans
    attrs = spans["fate.action.classified"].attributes
    assert attrs["actor"] == "Hero"
    assert attrs["action"] == "attack"
    assert attrs["skill"] == "Fight"
    assert attrs["target"] == "Thug"


def test_span_is_routed_for_the_gm_panel():
    # Registered as a typed state_transition route so the GM panel surfaces it.
    assert "fate.action.classified" in SPAN_ROUTES
    route = SPAN_ROUTES["fate.action.classified"]
    extracted = route.extract(
        type("S", (), {"attributes": {"actor": "Hero", "action": "overcome", "skill": "Notice"}})()
    )
    assert extracted["field"] == "action_classified"
    assert extracted["action"] == "overcome"
