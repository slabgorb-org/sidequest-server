"""Narrator OTEL spans: sealed-round emission and session-rotation lifecycle (ADR-066 §10)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute
from .span import Span

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPAN_NARRATOR_SEALED_ROUND = "narrator.sealed_round"
SPAN_NARRATOR_SESSION_ROTATED = "narrator.session_rotated"
SPAN_NARRATOR_UNRECOVERABLE = "narrator.unrecoverable"
# Story 49-3: Glenross playtest 2026-05-11 — narrator wrote new ``**Room
# Title**`` headers across five turns while character_locations stayed
# stale on ``the_manse``. ``_apply_narration_result_to_snapshot`` now
# auto-fills the structured patch field from the leading bold title and
# emits this span so Sebastien's GM panel surfaces every repair as the
# WARNING-level lie-detector signal the operator iterates the prompt on.
SPAN_NARRATOR_LOCATION_DRIFT_REPAIRED = "narrator.location_drift_repaired"

# Story 22-3: seed-context renderer span. Fires every narrator prompt
# build, even when both seed lists are empty — silence-by-absence is
# indistinguishable from renderer-not-invoked, so the always-emit signal
# is what lets the GM panel (22-4) prove injection engaged.
SPAN_NARRATOR_SEED_CONTEXT = "narrator.seed_context"

# Story 71-40: per-turn tool-loop observability. ``complete_with_tools`` runs
# one SDK round-trip per iteration; a turn that keeps requesting tools balloons
# solo-turn p95. The summary span records how many iterations a turn actually
# consumed (one-shot vs runaway), and the cap-hit span fires when a configurable
# ``iteration_cap`` (a soft warning threshold BELOW the hard
# ``AnthropicSdkLoopExceeded`` ceiling) is crossed — observability only, the
# fail-loud ceiling is unchanged.
SPAN_NARRATOR_TOOL_LOOP = "narrator.tool_loop"
SPAN_NARRATOR_TOOL_LOOP_CAP_HIT = "narrator.tool_loop.cap_hit"

FLAT_ONLY_SPANS.update(
    {
        SPAN_NARRATOR_SEALED_ROUND,
        SPAN_NARRATOR_SESSION_ROTATED,
        SPAN_NARRATOR_UNRECOVERABLE,
        SPAN_NARRATOR_SEED_CONTEXT,
    }
)

# Routed: the GM panel reads this as a typed ``state_transition`` row in
# the character-locations lane. ``op="location_drift_repaired"`` parallels
# the region-state op vocabulary so the dashboard can group the location-
# drift family alongside ``entry_rejected`` / ``canonicalized_dedup``.
SPAN_ROUTES[SPAN_NARRATOR_LOCATION_DRIFT_REPAIRED] = SpanRoute(
    event_type="state_transition",
    component="narrator",
    extract=lambda span: {
        "field": "character_locations",
        "op": "location_drift_repaired",
        "character": (span.attributes or {}).get("character", ""),
        "player_name": (span.attributes or {}).get("player_name", ""),
        "old_state": (span.attributes or {}).get("old_state", ""),
        "new_from_title": (span.attributes or {}).get("new_from_title", ""),
        "turn": (span.attributes or {}).get("turn", 0),
    },
)

# Routed so the WatcherSpanProcessor surfaces per-turn iteration counts on the
# GM panel — the lie-detector for "why is this solo turn slow" (Story 71-40).
SPAN_ROUTES[SPAN_NARRATOR_TOOL_LOOP] = SpanRoute(
    event_type="state_transition",
    component="narrator",
    extract=lambda span: {
        "field": "narrator.tool_loop",
        "iterations_used": (span.attributes or {}).get("iterations_used", 0),
        "max_iterations": (span.attributes or {}).get("max_iterations", 0),
    },
)

# Routed at WARNING grade: a crossed cap is a throttle signal the operator
# iterates the prompt/tools on, not a routine INFO breadcrumb (Story 71-40).
SPAN_ROUTES[SPAN_NARRATOR_TOOL_LOOP_CAP_HIT] = SpanRoute(
    event_type="state_transition",
    component="narrator",
    extract=lambda span: {
        "field": "narrator.tool_loop.cap_hit",
        "iteration_cap": (span.attributes or {}).get("iteration_cap", 0),
        "iterations_used": (span.attributes or {}).get("iterations_used", 0),
        "max_iterations": (span.attributes or {}).get("max_iterations", 0),
    },
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def narrator_session_rotated_span(
    *,
    reason: str,
    cumulative_tokens: int,
    turn_number: int,
    recap_chars: int,
    rebuild_latency_ms: int,
    threshold: int | None = None,
    cli_error_signature: str | None = None,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Emit narrator.session_rotated; reason ∈ {cli_error, session_expired, token_threshold, unknown}."""
    attrs: dict[str, Any] = {
        "reason": reason,
        "cumulative_tokens": cumulative_tokens,
        "turn_number": turn_number,
        "recap_chars": recap_chars,
        "rebuild_latency_ms": rebuild_latency_ms,
    }
    if threshold is not None:
        attrs["threshold"] = threshold
    if cli_error_signature is not None:
        attrs["cli_error_signature"] = cli_error_signature
    with Span.open(SPAN_NARRATOR_SESSION_ROTATED, attrs, tracer_override=_tracer) as span:
        yield span


@contextlib.contextmanager
def narrator_unrecoverable_span(
    *,
    reason: str,
    first_error_signature: str,
    rebuild_error_signature: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Emit narrator.unrecoverable when session rotation succeeds but the rebuild also fails (ADR-066 §8)."""
    attrs: dict[str, Any] = {
        "reason": reason,
        "first_error_signature": first_error_signature,
        "rebuild_error_signature": rebuild_error_signature,
        "turn_number": turn_number,
    }
    with Span.open(SPAN_NARRATOR_UNRECOVERABLE, attrs, tracer_override=_tracer) as span:
        span.set_status(trace.Status(trace.StatusCode.ERROR, "narrator unrecoverable"))
        yield span


@contextlib.contextmanager
def location_drift_repaired_span(
    *,
    old_state: str,
    new_from_title: str,
    character: str,
    player_name: str,
    turn: int,
    _tracer: trace.Tracer | None = None,
    **extra: Any,
) -> Iterator[trace.Span]:
    """Story 49-3: emitted when ``_apply_narration_result_to_snapshot``
    detected drift between the narrator's leading bold-title room header
    and the canonical ``character_locations`` entry, and auto-filled the
    structured patch field from the prose.

    ``severity="warning"`` opts the route translator into the warning
    grade so Sebastien's GM panel surfaces this above routine INFO
    state transitions — drift is a prompt-quality signal the operator
    iterates on, not a routine update.
    """
    attrs: dict[str, Any] = {
        "old_state": old_state,
        "new_from_title": new_from_title,
        "character": character,
        "player_name": player_name,
        "turn": turn,
        "severity": "warning",
        **extra,
    }
    with Span.open(SPAN_NARRATOR_LOCATION_DRIFT_REPAIRED, attrs, tracer_override=_tracer) as span:
        yield span


@contextlib.contextmanager
def narrator_tool_loop_span(
    *,
    iterations_used: int,
    max_iterations: int,
    _tracer: trace.Tracer | None = None,
    **extra: Any,
) -> Iterator[trace.Span]:
    """Story 71-40: per-turn tool-loop summary, fired once per successful turn.

    ``iterations_used`` is the number of SDK round-trips the turn consumed (1
    for a one-shot text turn, N for a turn that requested tools N-1 times before
    converging). The GM panel reads this to distinguish a runaway tool loop from
    a cheap one when diagnosing solo-turn p95.
    """
    attrs: dict[str, Any] = {
        "iterations_used": iterations_used,
        "max_iterations": max_iterations,
        **extra,
    }
    with Span.open(SPAN_NARRATOR_TOOL_LOOP, attrs, tracer_override=_tracer) as span:
        yield span


@contextlib.contextmanager
def narrator_tool_loop_cap_hit_span(
    *,
    iteration_cap: int,
    iterations_used: int,
    max_iterations: int,
    _tracer: trace.Tracer | None = None,
    **extra: Any,
) -> Iterator[trace.Span]:
    """Story 71-40: fires once when a turn crosses the soft ``iteration_cap``.

    The cap is a warning threshold BELOW the hard ``AnthropicSdkLoopExceeded``
    ceiling — crossing it records that the turn was unusually tool-heavy so the
    GM panel surfaces the throttled turn, WITHOUT weakening the fail-loud
    ceiling (the loop still raises at ``max_iterations``). ``severity="warning"``
    grades it above routine INFO transitions.
    """
    attrs: dict[str, Any] = {
        "iteration_cap": iteration_cap,
        "iterations_used": iterations_used,
        "max_iterations": max_iterations,
        "severity": "warning",
        **extra,
    }
    with Span.open(SPAN_NARRATOR_TOOL_LOOP_CAP_HIT, attrs, tracer_override=_tracer) as span:
        yield span


__all__ = [
    "SPAN_NARRATOR_LOCATION_DRIFT_REPAIRED",
    "SPAN_NARRATOR_SEALED_ROUND",
    "SPAN_NARRATOR_SEED_CONTEXT",
    "SPAN_NARRATOR_SESSION_ROTATED",
    "SPAN_NARRATOR_TOOL_LOOP",
    "SPAN_NARRATOR_TOOL_LOOP_CAP_HIT",
    "SPAN_NARRATOR_UNRECOVERABLE",
    "location_drift_repaired_span",
    "narrator_session_rotated_span",
    "narrator_tool_loop_cap_hit_span",
    "narrator_tool_loop_span",
    "narrator_unrecoverable_span",
]
