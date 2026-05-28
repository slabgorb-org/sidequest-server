from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans._core import SPAN_ROUTES
from sidequest.telemetry.spans.cwn import (
    SPAN_CWN_MAJOR_INJURY_ROLL,
    SPAN_CWN_MORTAL_INJURY_DECLARED,
    SPAN_CWN_SHOCK_APPLIED,
    SPAN_CWN_TRAUMA_ROLL,
    cwn_major_injury_roll_span,
    cwn_mortal_injury_declared_span,
    cwn_shock_applied_span,
    cwn_trauma_roll_span,
)


def _exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_all_four_spans_are_routed():
    for name in (
        SPAN_CWN_TRAUMA_ROLL,
        SPAN_CWN_SHOCK_APPLIED,
        SPAN_CWN_MORTAL_INJURY_DECLARED,
        SPAN_CWN_MAJOR_INJURY_ROLL,
    ):
        assert name in SPAN_ROUTES
        assert SPAN_ROUTES[name].component == "cwn"


def test_trauma_span_emits():
    exporter, tracer = _exporter()
    cwn_trauma_roll_span(actor="Mook", weapon_die="1d6", roll=6, target=6,
                         traumatic=True, rating=3, base=7, final=21, _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert spans[0].name == "cwn.trauma.roll"
    attrs = dict(spans[0].attributes or {})
    assert attrs["traumatic"] is True
    assert attrs["final"] == 21


def test_shock_span_emits():
    exporter, tracer = _exporter()
    cwn_shock_applied_span(actor="Mook", amount=2, melee_ac=8, shock_rating=10, _tracer=tracer)
    assert exporter.get_finished_spans()[0].name == "cwn.shock.applied"


def test_mortal_span_emits():
    exporter, tracer = _exporter()
    cwn_mortal_injury_declared_span(actor="Jax", rounds_to_die=6, _tracer=tracer)
    assert exporter.get_finished_spans()[0].name == "cwn.mortal_injury.declared"


def test_major_span_emits():
    exporter, tracer = _exporter()
    cwn_major_injury_roll_span(actor="Jax", save_made=False, roll=9,
                               text="Severed limb.", _tracer=tracer)
    spans = exporter.get_finished_spans()
    assert spans[0].name == "cwn.major_injury.roll"
    assert dict(spans[0].attributes or {})["roll"] == 9
