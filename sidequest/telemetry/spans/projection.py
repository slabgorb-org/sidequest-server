"""Projection spans — per-player event filtering and cache management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_PROJECTION_DECIDE = "projection.filter.decide"
SPAN_ROUTES[SPAN_PROJECTION_DECIDE] = SpanRoute(
    event_type="state_transition",
    component="projection",
    extract=lambda span: {
        "field": "projection.filter.decide",
        "player_id": (span.attributes or {}).get("player_id", ""),
        "event_kind": (span.attributes or {}).get("event.kind", ""),
        "event_seq": (span.attributes or {}).get("event.seq", 0),
        "decision_include": (span.attributes or {}).get("decision.include", None),
        "rule_source": (span.attributes or {}).get("rule.source", ""),
    },
)
SPAN_PROJECTION_CACHE_FILL = "projection.cache.fill"
SPAN_ROUTES[SPAN_PROJECTION_CACHE_FILL] = SpanRoute(
    event_type="state_transition",
    component="projection",
    extract=lambda span: {
        "field": "projection.cache.fill",
        "player_id": (span.attributes or {}).get("player_id", ""),
        "event_seq": (span.attributes or {}).get("event.seq", 0),
    },
)
SPAN_PROJECTION_CACHE_LAZY_FILL = "projection.cache.lazy_fill"
SPAN_ROUTES[SPAN_PROJECTION_CACHE_LAZY_FILL] = SpanRoute(
    event_type="state_transition",
    component="projection",
    extract=lambda span: {
        "field": "projection.cache.lazy_fill",
        "player_id": (span.attributes or {}).get("player_id", ""),
        "events_filled": (span.attributes or {}).get("events_filled", 0),
    },
)


@contextmanager
def projection_decide_span(
    *,
    event_kind: str,
    event_seq: int | None,
    player_id: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    attributes: dict[str, Any] = {"event.kind": event_kind, "player_id": player_id}
    if event_seq is not None:
        attributes["event.seq"] = event_seq
    with Span.open(SPAN_PROJECTION_DECIDE, attributes, tracer_override=_tracer) as span:
        yield span


@contextmanager
def projection_cache_fill_span(
    *, event_seq: int, player_id: str, _tracer: trace.Tracer | None = None
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_PROJECTION_CACHE_FILL,
        {"event.seq": event_seq, "player_id": player_id},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def projection_cache_lazy_fill_span(
    *, player_id: str, _tracer: trace.Tracer | None = None
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_PROJECTION_CACHE_LAZY_FILL,
        {"player_id": player_id},
        tracer_override=_tracer,
    ) as span:
        yield span


# ---------------------------------------------------------------------------
# Cache retention prune — Story 126-22. projection_cache is a pure cache (it
# lazy-fills from the event log on reconnect), so a per-(session, player)
# keep-last-N window is bounded and recoverable. The GM panel / save-forensics
# must SEE the prune fire (rows_pruned + session_id) rather than the table
# silently shrinking — otherwise an over- or under-aggressive bound is
# invisible (CLAUDE.md OTEL Observability Principle).
# ---------------------------------------------------------------------------
SPAN_PROJECTION_CACHE_PRUNE = "projection.cache.prune"
SPAN_ROUTES[SPAN_PROJECTION_CACHE_PRUNE] = SpanRoute(
    event_type="state_transition",
    component="projection",
    extract=lambda span: {
        "field": "projection.cache.prune",
        "session_id": (span.attributes or {}).get("session_id", 0),
        "rows_pruned": (span.attributes or {}).get("rows_pruned", 0),
    },
)


@contextmanager
def projection_cache_prune_span(
    *, session_id: int, _tracer: trace.Tracer | None = None
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_PROJECTION_CACHE_PRUNE,
        {"session_id": session_id},
        tracer_override=_tracer,
    ) as span:
        yield span
