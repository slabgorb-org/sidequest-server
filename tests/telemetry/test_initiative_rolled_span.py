"""SWN P4: the initiative roll emits an OTEL span (the polygraph) so the GM
panel can verify the order is engine-rolled, not narrator improv.

Mirrors ``encounter_resolved_span`` (constant + ``SPAN_ROUTES`` registration +
``Span.open(..., tracer_override=_tracer)`` contextmanager) and asserts span
attributes via the same ``otel_capture`` fixture idiom as
``test_lethality_span.py`` (install an InMemorySpanExporter on the live
TracerProvider so we observe what production code actually emits).
"""

from __future__ import annotations

import pytest

from sidequest.telemetry.spans._core import SPAN_ROUTES
from sidequest.telemetry.spans.encounter import (
    SPAN_ENCOUNTER_INITIATIVE_ROLLED,
    encounter_initiative_rolled_span,
)


@pytest.fixture
def otel_capture():
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def test_initiative_rolled_span_constant_declared():
    assert SPAN_ENCOUNTER_INITIATIVE_ROLLED == "encounter.initiative_rolled"


def test_initiative_rolled_span_route_registered():
    assert SPAN_ENCOUNTER_INITIATIVE_ROLLED in SPAN_ROUTES
    route = SPAN_ROUTES[SPAN_ENCOUNTER_INITIATIVE_ROLLED]
    assert route.event_type == "state_transition"
    assert route.component == "encounter"
    extracted = route.extract(
        type(
            "S",
            (),
            {
                "attributes": {
                    "encounter_type": "firefight",
                    "initiative_order": "Rux(9), Raider(5)",
                    "source": "instantiate",
                }
            },
        )()
    )
    assert extracted["encounter_type"] == "firefight"
    assert extracted["initiative_order"] == "Rux(9), Raider(5)"
    assert extracted["source"] == "instantiate"


def test_initiative_rolled_span_carries_order(otel_capture):
    with encounter_initiative_rolled_span(
        encounter_type="firefight",
        initiative_order="Rux(9), Raider(5)",
        source="instantiate",
    ):
        pass

    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_ENCOUNTER_INITIATIVE_ROLLED
    ]
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "encounter.initiative_rolled"
    assert span.attributes["encounter_type"] == "firefight"
    assert span.attributes["initiative_order"] == "Rux(9), Raider(5)"
    assert span.attributes["source"] == "instantiate"
