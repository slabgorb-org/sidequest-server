from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans._core import SPAN_ROUTES
from sidequest.telemetry.spans.cwn import (
    SPAN_CWN_SYSTEM_STRAIN_DELTA,
    cwn_system_strain_delta_span,
)


def test_strain_span_is_routed():
    assert SPAN_CWN_SYSTEM_STRAIN_DELTA == "cwn.system_strain.delta"
    assert SPAN_CWN_SYSTEM_STRAIN_DELTA in SPAN_ROUTES
    route = SPAN_ROUTES[SPAN_CWN_SYSTEM_STRAIN_DELTA]
    assert route.component == "cwn"


def test_strain_span_emits_attributes():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    cwn_system_strain_delta_span(
        actor="Jax",
        source="cyberarm_install",
        amount=3,
        new_total=3,
        max=14,
        applied=True,
        _tracer=tracer,
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert spans[0].name == "cwn.system_strain.delta"
    assert attrs["actor"] == "Jax"
    assert attrs["source"] == "cyberarm_install"
    assert attrs["amount"] == 3
    assert attrs["new_total"] == 3
    assert attrs["max"] == 14
    assert attrs["applied"] is True
