from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans.cwn import (
    SPAN_CWN_HACKING_SECURITY_CHECK,
    cwn_hacking_security_check_span,
)


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_hacking_span_name_and_attributes():
    exporter, tracer = _exporter()
    cwn_hacking_security_check_span(
        actor="Rux",
        verb="Run Program",
        tier="black_site",
        base_dc=12,
        alert_modifier=2,
        effective_dc=14,
        result="Success",
        _tracer=tracer,
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == SPAN_CWN_HACKING_SECURITY_CHECK == "cwn.hacking.security_check"
    attrs = dict(spans[0].attributes or {})
    assert attrs["actor"] == "Rux"
    assert attrs["verb"] == "Run Program"
    assert attrs["tier"] == "black_site"
    assert attrs["base_dc"] == 12
    assert attrs["alert_modifier"] == 2
    assert attrs["effective_dc"] == 14
    assert attrs["result"] == "Success"


def test_hacking_span_route_registered():
    from sidequest.telemetry.spans.cwn import SPAN_ROUTES

    route = SPAN_ROUTES[SPAN_CWN_HACKING_SECURITY_CHECK]
    assert route.event_type == "state_transition"
    assert route.component == "cwn"
