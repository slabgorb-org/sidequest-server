"""The fate.item_promoted span (GM panel = lie detector): a gained item became
an invokable aspect on the FateSheet, or was a logged dedup no-op."""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans._core import SPAN_ROUTES
from sidequest.telemetry.spans.fate import fate_item_promoted_span


class _FakeSpan:
    def __init__(self, attributes: dict) -> None:
        self.attributes = attributes


def test_item_promoted_route_registered_and_maps_fields():
    route = SPAN_ROUTES["fate.item_promoted"]
    assert route.component == "fate"
    assert route.event_type == "state_transition"
    fields = route.extract(
        _FakeSpan(
            {
                "actor": "Dorothy",
                "item_id": "narrator:silver_shoes",
                "item_name": "Silver Shoes",
                "aspect_text": "The Silver Shoes of the Dead Witch",
                "source": "catalog",
                "aspects_added": 1,
                "stunts_deferred": 0,
                "deduped": False,
            }
        )
    )
    assert fields["source"] == "catalog"
    assert fields["aspects_added"] == 1
    assert fields["stunts_deferred"] == 0
    assert fields["deduped"] is False


def test_item_promoted_emitter_fires_named_span():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    fate_item_promoted_span(
        actor="Dorothy",
        item_id="narrator:silver_shoes",
        item_name="Silver Shoes",
        aspect_text="The Silver Shoes of the Dead Witch",
        source="catalog",
        aspects_added=1,
        stunts_deferred=0,
        deduped=False,
        _tracer=tracer,
    )

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["fate.item_promoted"]
    assert spans[0].attributes["source"] == "catalog"
    assert spans[0].attributes["item_id"] == "narrator:silver_shoes"
