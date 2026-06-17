from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans._core import SPAN_ROUTES
from sidequest.telemetry.spans.fate import (
    fate_contest_exchange_span,
    fate_contest_resolved_span,
    fate_contest_seeded_span,
)


def _exporter():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("t"), exporter


def test_contest_spans_emit_and_route():
    tracer, exporter = _exporter()
    fate_contest_seeded_span(encounter_type="negotiation", target=3, player_seats=1, _tracer=tracer)
    fate_contest_exchange_span(
        winner_side="player",
        victory_delta=2,
        player_victories=2,
        opponent_victories=0,
        round_number=1,
        _tracer=tracer,
    )
    fate_contest_resolved_span(
        winner_side="player", player_victories=3, opponent_victories=1, _tracer=tracer
    )
    names = {s.name for s in exporter.get_finished_spans()}
    assert {"fate.contest.seeded", "fate.contest.exchange", "fate.contest.resolved"} <= names
    # Each contest span must carry a GM-panel route.
    for key in ("fate.contest.seeded", "fate.contest.exchange", "fate.contest.resolved"):
        assert key in SPAN_ROUTES, f"{key} has no SPAN_ROUTES entry"
