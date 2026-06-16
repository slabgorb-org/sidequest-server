"""Tests for narrator streaming OTEL spans."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_spans.py)
# ---------------------------------------------------------------------------


def _fresh_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Return a TracerProvider backed by an in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _local_tracer(provider: TracerProvider) -> trace.Tracer:
    """Get a tracer scoped to a specific provider (avoids global provider lock)."""
    return provider.get_tracer("test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stream_start_span_records_turn_id_and_prompt_tokens():
    from sidequest.telemetry.spans import narrator_stream_start_span

    provider, exporter = _fresh_provider()
    t = _local_tracer(provider)

    with narrator_stream_start_span(
        turn_id="t-1",
        prompt_tokens=100,
        model="claude-opus-4-7",
        session_id="s-1",
        _tracer=t,
    ):
        pass

    spans = exporter.get_finished_spans()
    [span] = [s for s in spans if s.name == "narrator.stream.start"]
    assert span.attributes["turn_id"] == "t-1"
    assert span.attributes["prompt_tokens"] == 100
    assert span.attributes["model"] == "claude-opus-4-7"
    assert span.attributes["session_id"] == "s-1"


def test_stream_start_span_omits_session_id_when_none():
    from sidequest.telemetry.spans import narrator_stream_start_span

    provider, exporter = _fresh_provider()
    t = _local_tracer(provider)

    with narrator_stream_start_span(
        turn_id="t-2",
        prompt_tokens=50,
        model="claude-opus-4-7",
        session_id=None,
        _tracer=t,
    ):
        pass

    spans = exporter.get_finished_spans()
    [span] = [s for s in spans if s.name == "narrator.stream.start"]
    assert "session_id" not in (span.attributes or {})


def test_stream_first_token_records_ttft():
    from sidequest.telemetry.spans import narrator_stream_first_token

    provider, exporter = _fresh_provider()
    t = _local_tracer(provider)

    narrator_stream_first_token(turn_id="t-1", ttft_seconds=1.234, _tracer=t)

    spans = exporter.get_finished_spans()
    [span] = [s for s in spans if s.name == "narrator.stream.first_token"]
    assert span.attributes["turn_id"] == "t-1"
    assert span.attributes["ttft_seconds"] == 1.234


def test_stream_fence_detected_records_offset_and_timing():
    from sidequest.telemetry.spans import narrator_stream_fence_detected

    provider, exporter = _fresh_provider()
    t = _local_tracer(provider)

    narrator_stream_fence_detected(
        turn_id="t-1",
        prose_bytes_at_fence=1500,
        seconds_to_fence=2.5,
        _tracer=t,
    )

    spans = exporter.get_finished_spans()
    [span] = [s for s in spans if s.name == "narrator.stream.fence_detected"]
    assert span.attributes["turn_id"] == "t-1"
    assert span.attributes["prose_bytes_at_fence"] == 1500
    assert span.attributes["seconds_to_fence"] == 2.5


def test_stream_complete_records_status_and_metrics():
    from sidequest.telemetry.spans import narrator_stream_complete_span

    provider, exporter = _fresh_provider()
    t = _local_tracer(provider)

    narrator_stream_complete_span(
        turn_id="t-1",
        total_seconds=5.0,
        ttft_seconds=1.2,
        prose_bytes=1500,
        delta_count=42,
        json_parse_status="complete",
        input_tokens=100,
        output_tokens=50,
        _tracer=t,
    )

    spans = exporter.get_finished_spans()
    [span] = [s for s in spans if s.name == "narrator.stream.complete"]
    assert span.attributes["json_parse_status"] == "complete"
    assert span.attributes["delta_count"] == 42
    assert span.attributes["total_seconds"] == 5.0
    assert span.attributes["ttft_seconds"] == 1.2
    assert span.attributes["prose_bytes"] == 1500
    assert span.attributes["input_tokens"] == 100
    assert span.attributes["output_tokens"] == 50


def test_stream_complete_omits_optional_none_attrs():
    from sidequest.telemetry.spans import narrator_stream_complete_span

    provider, exporter = _fresh_provider()
    t = _local_tracer(provider)

    narrator_stream_complete_span(
        turn_id="t-1",
        total_seconds=3.0,
        ttft_seconds=None,
        prose_bytes=800,
        delta_count=10,
        json_parse_status="no_fence",
        input_tokens=None,
        output_tokens=None,
        _tracer=t,
    )

    spans = exporter.get_finished_spans()
    [span] = [s for s in spans if s.name == "narrator.stream.complete"]
    assert "ttft_seconds" not in (span.attributes or {})
    assert "input_tokens" not in (span.attributes or {})
    assert "output_tokens" not in (span.attributes or {})


def test_stream_error_records_error_kind_and_status():
    from sidequest.telemetry.spans import narrator_stream_error_span

    provider, exporter = _fresh_provider()
    t = _local_tracer(provider)

    narrator_stream_error_span(
        turn_id="t-1",
        error_kind="timeout",
        partial_prose_bytes=200,
        total_seconds=120.0,
        detail="claude CLI timed out",
        _tracer=t,
    )

    spans = exporter.get_finished_spans()
    [span] = [s for s in spans if s.name == "narrator.stream.error"]
    assert span.attributes["error_kind"] == "timeout"
    assert span.attributes["partial_prose_bytes"] == 200
    assert span.attributes["total_seconds"] == 120.0
    assert span.attributes["detail"] == "claude CLI timed out"
    # status should be ERROR
    assert span.status.status_code == trace.StatusCode.ERROR


def test_stream_error_truncates_long_detail():
    from sidequest.telemetry.spans import narrator_stream_error_span

    provider, exporter = _fresh_provider()
    t = _local_tracer(provider)

    long_detail = "x" * 600
    narrator_stream_error_span(
        turn_id="t-1",
        error_kind="unknown",
        partial_prose_bytes=0,
        total_seconds=1.0,
        detail=long_detail,
        _tracer=t,
    )

    spans = exporter.get_finished_spans()
    [span] = [s for s in spans if s.name == "narrator.stream.error"]
    assert len(span.attributes["detail"]) == 500


def test_stream_cancelled_records_reason():
    from sidequest.telemetry.spans import narrator_stream_cancelled_span

    provider, exporter = _fresh_provider()
    t = _local_tracer(provider)

    narrator_stream_cancelled_span(
        turn_id="t-1",
        reason="player_interrupt",
        partial_prose_bytes=300,
        _tracer=t,
    )

    spans = exporter.get_finished_spans()
    [span] = [s for s in spans if s.name == "narrator.stream.cancelled"]
    assert span.attributes["reason"] == "player_interrupt"
    assert span.attributes["partial_prose_bytes"] == 300
    assert span.attributes["turn_id"] == "t-1"


def test_stream_span_constants_importable():
    """Regression gate: all 6 streaming span constants are importable and non-empty."""
    from sidequest.telemetry.spans import (
        SPAN_NARRATOR_STREAM_CANCELLED,
        SPAN_NARRATOR_STREAM_COMPLETE,
        SPAN_NARRATOR_STREAM_ERROR,
        SPAN_NARRATOR_STREAM_FENCE_DETECTED,
        SPAN_NARRATOR_STREAM_FIRST_TOKEN,
        SPAN_NARRATOR_STREAM_START,
    )

    constants = [
        SPAN_NARRATOR_STREAM_START,
        SPAN_NARRATOR_STREAM_FIRST_TOKEN,
        SPAN_NARRATOR_STREAM_FENCE_DETECTED,
        SPAN_NARRATOR_STREAM_COMPLETE,
        SPAN_NARRATOR_STREAM_ERROR,
        SPAN_NARRATOR_STREAM_CANCELLED,
    ]
    for c in constants:
        assert isinstance(c, str) and len(c) > 0
