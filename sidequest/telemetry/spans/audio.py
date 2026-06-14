"""Audio + music spans — backend lifecycle and per-turn cue dispatch."""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# Audio backend lifecycle + per-turn dispatch
SPAN_AUDIO_BACKEND_ENABLED = "audio.backend_enabled"
SPAN_ROUTES[SPAN_AUDIO_BACKEND_ENABLED] = SpanRoute(
    event_type="state_transition",
    component="audio",
    extract=lambda span: {
        "field": "audio",
        "op": "enabled",
        "genre": (span.attributes or {}).get("genre", ""),
        "mood_count": (span.attributes or {}).get("mood_count", 0),
        "sfx_count": (span.attributes or {}).get("sfx_count", 0),
    },
)
SPAN_AUDIO_BACKEND_DISABLED = "audio.backend_disabled"
SPAN_ROUTES[SPAN_AUDIO_BACKEND_DISABLED] = SpanRoute(
    event_type="state_transition",
    component="audio",
    extract=lambda span: {
        "field": "audio",
        "op": "disabled",
        "reason": (span.attributes or {}).get("reason", ""),
        "genre": (span.attributes or {}).get("genre", ""),
    },
)
SPAN_AUDIO_SKIPPED = "audio.skipped"
SPAN_ROUTES[SPAN_AUDIO_SKIPPED] = SpanRoute(
    event_type="state_transition",
    component="audio",
    extract=lambda span: {
        "field": "audio",
        "op": "skipped",
        "reason": (span.attributes or {}).get("reason", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
        "extra": (span.attributes or {}).get("extra_json", "{}"),
    },
)
SPAN_AUDIO_DISPATCHED = "audio.dispatched"
SPAN_ROUTES[SPAN_AUDIO_DISPATCHED] = SpanRoute(
    event_type="state_transition",
    component="audio",
    extract=lambda span: {
        "field": "audio",
        "op": "dispatched",
        "turn_number": (span.attributes or {}).get("turn_number", 0),
        "mood": (span.attributes or {}).get("mood", ""),
        "music_track": (span.attributes or {}).get("music_track", ""),
        "sfx_count": (span.attributes or {}).get("sfx_count", 0),
    },
)


SPAN_MUSIC_MOOD_ALIAS_RESOLVED = "music.mood_alias_resolved"
SPAN_ROUTES[SPAN_MUSIC_MOOD_ALIAS_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="audio",
    extract=lambda span: {
        "field": "music",
        "op": "mood_alias_resolved",
        "mood_name": (span.attributes or {}).get("mood_name", ""),
        "resolved_to": (span.attributes or {}).get("resolved_to", ""),
        "chain_depth": (span.attributes or {}).get("chain_depth", 0),
    },
)
SPAN_MUSIC_MOOD_ALIAS_FAILED = "music.mood_alias_failed"
SPAN_ROUTES[SPAN_MUSIC_MOOD_ALIAS_FAILED] = SpanRoute(
    event_type="state_transition",
    component="audio",
    extract=lambda span: {
        "field": "music",
        "op": "mood_alias_failed",
        "mood_name": (span.attributes or {}).get("mood_name", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "fallback_mood": (span.attributes or {}).get("fallback_mood", ""),
    },
)


@contextmanager
def mood_alias_resolved_span(
    *,
    mood_name: str,
    resolved_to: str,
    chain_depth: int,
    latency_ms: float,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """An alias mood resolved to a real mood_tracks key via the alias chain."""
    attributes: dict[str, Any] = {
        "mood_name": mood_name,
        "resolved_to": resolved_to,
        "chain_depth": chain_depth,
        "latency_ms": latency_ms,
        **attrs,
    }
    with Span.open(SPAN_MUSIC_MOOD_ALIAS_RESOLVED, attributes, tracer_override=_tracer) as span:
        yield span


@contextmanager
def mood_alias_failed_span(
    *,
    mood_name: str,
    reason: str,
    fallback_mood: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Mood resolution fell back to the default.

    ``reason``: ``broken_chain`` (runtime — unknown/undeclared mood) |
    ``loop_detected`` | ``depth_exceeded`` (load-time classification, surfaced
    in the raised pack-load error rather than a runtime span).
    """
    attributes: dict[str, Any] = {
        "mood_name": mood_name,
        "reason": reason,
        "fallback_mood": fallback_mood,
        **attrs,
    }
    with Span.open(SPAN_MUSIC_MOOD_ALIAS_FAILED, attributes, tracer_override=_tracer) as span:
        yield span


@contextmanager
def audio_backend_enabled_span(
    *,
    genre: str,
    mood_count: int,
    sfx_count: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    attributes: dict[str, Any] = {
        "genre": genre,
        "mood_count": mood_count,
        "sfx_count": sfx_count,
        **attrs,
    }
    with Span.open(SPAN_AUDIO_BACKEND_ENABLED, attributes, tracer_override=_tracer) as span:
        yield span


@contextmanager
def audio_backend_disabled_span(
    *,
    reason: str,
    genre: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """``reason``: ``pack_dir_missing`` | ``empty_config``."""
    attributes: dict[str, Any] = {"reason": reason, "genre": genre, **attrs}
    with Span.open(SPAN_AUDIO_BACKEND_DISABLED, attributes, tracer_override=_tracer) as span:
        yield span


@contextmanager
def audio_skipped_span(
    *,
    reason: str,
    turn_number: int,
    extra: dict[str, object] | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """``extra`` is JSON-encoded — OTEL silently drops dict attribute values."""
    attributes: dict[str, Any] = {
        "reason": reason,
        "turn_number": turn_number,
        "extra_json": _json.dumps(dict(extra or {}), sort_keys=True),
        **attrs,
    }
    with Span.open(SPAN_AUDIO_SKIPPED, attributes, tracer_override=_tracer) as span:
        yield span


@contextmanager
def audio_dispatched_span(
    *,
    turn_number: int,
    mood: str,
    music_track: str,
    sfx_count: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    attributes: dict[str, Any] = {
        "turn_number": turn_number,
        "mood": mood,
        "music_track": music_track,
        "sfx_count": sfx_count,
        **attrs,
    }
    with Span.open(SPAN_AUDIO_DISPATCHED, attributes, tracer_override=_tracer) as span:
        yield span
