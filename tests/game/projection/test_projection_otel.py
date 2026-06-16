"""OTEL spans emitted by the projection pipeline."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import SessionGameStateView


def _setup_tracing() -> InMemorySpanExporter:
    """Attach an in-memory exporter to whichever TracerProvider is active.

    OTEL forbids replacing a TracerProvider once set; attach to the current
    provider if one is installed, otherwise install a fresh one.
    """
    exporter = InMemorySpanExporter()
    current = trace.get_tracer_provider()
    if hasattr(current, "add_span_processor"):
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    return exporter


def test_decide_span_emitted_with_attributes() -> None:
    exporter = _setup_tracing()
    filt = ComposedFilter.with_no_genre_rules()
    view = SessionGameStateView(gm_player_id="gm", player_id_to_character={"alice": "alice_char"})
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"hi"}', origin_seq=42)

    filt.project(envelope=env, view=view, player_id="alice")

    decide_spans = [
        s for s in exporter.get_finished_spans() if s.name == "projection.filter.decide"
    ]
    assert len(decide_spans) == 1
    attrs = dict(decide_spans[0].attributes or {})
    assert attrs["event.kind"] == "NARRATION"
    assert attrs["event.seq"] == 42
    assert attrs["player_id"] == "alice"
    assert attrs["decision.include"] is True
    assert attrs["rule.source"] == "default:pass_through"
