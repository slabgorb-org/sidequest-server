"""Tests for dungeon.quest.* OTEL spans (per-expansion quest bind + resolve).

Mirrors the in-memory exporter pattern from test_setpiece_attach_wiring.py /
test_persistence.py.
"""
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sidequest.telemetry.spans.dungeon_quest import (
    quest_bound_span, quest_resolved_span, SPAN_QUEST_BOUND, SPAN_QUEST_RESOLVED,
)


def _capture():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_quest_bound_span_emits_attributes():
    exporter, tracer = _capture()
    with quest_bound_span(expansion_id=1, signature_kind="reach_deep",
                          ref_id="exp001.r3", degraded=True, _tracer=tracer):
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == SPAN_QUEST_BOUND
    assert spans[0].attributes["signature_kind"] == "reach_deep"
    assert spans[0].attributes["degraded"] is True


def test_quest_resolved_span_emits():
    exporter, tracer = _capture()
    with quest_resolved_span(expansion_id=1, signature_kind="big_bad",
                             resolving_event="hp_depletion", _tracer=tracer):
        pass
    spans = exporter.get_finished_spans()
    assert spans[0].name == SPAN_QUEST_RESOLVED
    assert spans[0].attributes["resolving_event"] == "hp_depletion"


def test_quest_bound_span_in_span_routes():
    """Wiring test — both quest spans are registered in SPAN_ROUTES."""
    from sidequest.telemetry.spans import SPAN_ROUTES  # noqa: PLC0415

    assert SPAN_QUEST_BOUND in SPAN_ROUTES, (
        f"{SPAN_QUEST_BOUND!r} not in SPAN_ROUTES — GM panel cannot see quest binds"
    )
    assert SPAN_QUEST_RESOLVED in SPAN_ROUTES, (
        f"{SPAN_QUEST_RESOLVED!r} not in SPAN_ROUTES — GM panel cannot see quest resolutions"
    )
