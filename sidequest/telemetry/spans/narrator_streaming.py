"""Narrator streaming lifecycle spans.

Covers the streaming narration pipeline added in Story 2 / Task 12:
  narrator.stream.start        — wraps the entire streaming turn
  narrator.stream.first_token  — one-shot on first TextDelta (TTFT)
  narrator.stream.fence_detected — prose→JSON transition point
  narrator.stream.complete     — terminal: successful stream
  narrator.stream.error        — terminal: StreamError or exception
  narrator.stream.cancelled    — terminal: asyncio.CancelledError
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS
from .span import Span

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPAN_NARRATOR_STREAM_START = "narrator.stream.start"
SPAN_NARRATOR_STREAM_FIRST_TOKEN = "narrator.stream.first_token"
SPAN_NARRATOR_STREAM_FENCE_DETECTED = "narrator.stream.fence_detected"
SPAN_NARRATOR_STREAM_COMPLETE = "narrator.stream.complete"
SPAN_NARRATOR_STREAM_ERROR = "narrator.stream.error"
SPAN_NARRATOR_STREAM_CANCELLED = "narrator.stream.cancelled"

FLAT_ONLY_SPANS.update(
    {
        SPAN_NARRATOR_STREAM_START,
        SPAN_NARRATOR_STREAM_FIRST_TOKEN,
        SPAN_NARRATOR_STREAM_FENCE_DETECTED,
        SPAN_NARRATOR_STREAM_COMPLETE,
        SPAN_NARRATOR_STREAM_ERROR,
        SPAN_NARRATOR_STREAM_CANCELLED,
    }
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def narrator_stream_start_span(
    *,
    turn_id: str,
    prompt_tokens: int,
    model: str,
    session_id: str | None,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Context manager wrapping the entire streaming turn.

    Emit as ``narrator.stream.start``. All other streaming spans are nested
    (or emitted as siblings) within this lifetime.
    """
    attrs: dict[str, Any] = {
        "turn_id": turn_id,
        "prompt_tokens": prompt_tokens,
        "model": model,
    }
    if session_id is not None:
        attrs["session_id"] = session_id
    with Span.open(SPAN_NARRATOR_STREAM_START, attrs, tracer_override=_tracer) as span:
        yield span


def narrator_stream_first_token(
    *,
    turn_id: str,
    ttft_seconds: float,
    _tracer: trace.Tracer | None = None,
) -> None:
    """One-shot span emitted on the first TextDelta (time-to-first-token)."""
    with Span.open(
        SPAN_NARRATOR_STREAM_FIRST_TOKEN,
        {"turn_id": turn_id, "ttft_seconds": ttft_seconds},
        tracer_override=_tracer,
    ):
        pass


def narrator_stream_fence_detected(
    *,
    turn_id: str,
    prose_bytes_at_fence: int,
    seconds_to_fence: float,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Emitted when StreamFenceParser transitions PROSE → JSON_BUFFERING."""
    with Span.open(
        SPAN_NARRATOR_STREAM_FENCE_DETECTED,
        {
            "turn_id": turn_id,
            "prose_bytes_at_fence": prose_bytes_at_fence,
            "seconds_to_fence": seconds_to_fence,
        },
        tracer_override=_tracer,
    ):
        pass


def narrator_stream_complete_span(
    *,
    turn_id: str,
    total_seconds: float,
    ttft_seconds: float | None,
    prose_bytes: int,
    delta_count: int,
    json_parse_status: str,
    input_tokens: int | None,
    output_tokens: int | None,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Terminal span for a successful streaming turn."""
    attrs: dict[str, Any] = {
        "turn_id": turn_id,
        "total_seconds": total_seconds,
        "prose_bytes": prose_bytes,
        "delta_count": delta_count,
        "json_parse_status": json_parse_status,
    }
    if ttft_seconds is not None:
        attrs["ttft_seconds"] = ttft_seconds
    if input_tokens is not None:
        attrs["input_tokens"] = input_tokens
    if output_tokens is not None:
        attrs["output_tokens"] = output_tokens
    with Span.open(SPAN_NARRATOR_STREAM_COMPLETE, attrs, tracer_override=_tracer):
        pass


def narrator_stream_error_span(
    *,
    turn_id: str,
    error_kind: str,
    partial_prose_bytes: int,
    total_seconds: float,
    detail: str,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Terminal span for a failed streaming turn (StreamError or exception)."""
    with Span.open(
        SPAN_NARRATOR_STREAM_ERROR,
        {
            "turn_id": turn_id,
            "error_kind": error_kind,
            "partial_prose_bytes": partial_prose_bytes,
            "total_seconds": total_seconds,
            "detail": detail[:500],
        },
        tracer_override=_tracer,
    ) as span:
        span.set_status(trace.Status(trace.StatusCode.ERROR, detail))


def narrator_stream_cancelled_span(
    *,
    turn_id: str,
    reason: str,
    partial_prose_bytes: int,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Terminal span for a cancelled streaming turn (e.g. player interrupt)."""
    with Span.open(
        SPAN_NARRATOR_STREAM_CANCELLED,
        {
            "turn_id": turn_id,
            "reason": reason,
            "partial_prose_bytes": partial_prose_bytes,
        },
        tracer_override=_tracer,
    ):
        pass
