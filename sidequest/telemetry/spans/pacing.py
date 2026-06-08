"""OTEL span for the per-turn pacing hint (Story 81-3, ADR-025).

``_build_turn_context`` derives a :class:`~sidequest.game.tension_tracker.PacingHint`
from the per-session ``TensionTracker`` (story 81-2) and stamps it onto the
``TurnContext`` so the ``[PACING]`` narrator section fires. This span is the
GM-panel lie detector for that decision: it records the computed drama weight,
the suggested sentence count, the delivery mode, and whether an escalation beat
triggered — so the dev can verify the hint the narrator received was computed
from real tension state, not improvised. Routed under the ``tension`` component
to sit alongside the 81-2 ``tension:round_observed`` signal on the panel.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_PACING_HINT = "pacing.hint_computed"
SPAN_ROUTES[SPAN_PACING_HINT] = SpanRoute(
    event_type="state_transition",
    component="tension",
    extract=lambda span: {
        "field": "pacing_hint",
        "op": "hint_computed",
        "drama_weight": (span.attributes or {}).get("drama_weight", 0.0),
        "target_sentences": (span.attributes or {}).get("target_sentences", 0),
        "delivery_mode": (span.attributes or {}).get("delivery_mode", ""),
        "escalation_present": (span.attributes or {}).get("escalation_present", False),
    },
)


@contextmanager
def pacing_hint_span(
    *,
    drama_weight: float,
    target_sentences: int,
    delivery_mode: str,
    escalation_present: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 81-3: emitted once per ``_build_turn_context`` when a pacing hint
    is derived from the session ``TensionTracker``.

    Attributes mirror the :class:`PacingHint` fields the narrator section
    renders, so the GM panel can confirm the hint that reached the prompt is
    the one the tension state computed. ``escalation_present`` is a bool rather
    than the beat string because OTEL attributes reject ``None`` values.
    """
    attributes: dict[str, Any] = {
        "drama_weight": drama_weight,
        "target_sentences": target_sentences,
        "delivery_mode": delivery_mode,
        "escalation_present": escalation_present,
        **attrs,
    }
    with Span.open(SPAN_PACING_HINT, attributes, tracer_override=_tracer) as span:
        yield span


# Story 77-7 (ADR-024/025/128) — engine lull-escalation. When the game lulls
# (TensionTracker boring_streak >= the genre's escalation_streak), the selector
# in ``sidequest.game.lull_escalation`` fires a seed as a concrete escalation
# directive for the next turn. This span is the GM-panel lie detector for that
# decision — the proof the ENGINE pushed a Bang rather than the narrator
# improvising one. Routed under the same ``tension`` component as
# ``SPAN_PACING_HINT`` so it lights the pacing/tension subsystem grid, and it
# fires on every engaged run (fire / cooldown / none_available) carrying the
# five decision fields the panel renders.
SPAN_LULL_ESCALATION = "pacing.lull_escalation"
SPAN_ROUTES[SPAN_LULL_ESCALATION] = SpanRoute(
    event_type="state_transition",
    component="tension",
    extract=lambda span: {
        "field": "lull_escalation",
        "op": "lull_escalation",
        "boring_streak": (span.attributes or {}).get("boring_streak", 0),
        "drama_weight": (span.attributes or {}).get("drama_weight", 0.0),
        "fired": (span.attributes or {}).get("fired", False),
        "selected_seed_id": (span.attributes or {}).get("selected_seed_id", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        # sq-playtest 2026-06-07 (77-7 forensics): session attribution so the
        # GM panel can tell WHICH session's deck declined, not just that one did.
        "session_slug": (span.attributes or {}).get("session_slug", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "world_slug": (span.attributes or {}).get("world_slug", ""),
    },
)


@contextmanager
def lull_escalation_span(
    *,
    boring_streak: int,
    drama_weight: float,
    fired: bool,
    selected_seed_id: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 77-7: emitted once per engaged lull-escalation run.

    ``selected_seed_id`` is the empty string (not ``None``) when nothing fired —
    OTEL attributes reject ``None`` (same convention as ``escalation_present``
    being a bool above). ``reason`` is one of ``fired`` / ``cooldown`` /
    ``none_available``; the below-threshold no-op emits no span at all.
    """
    attributes: dict[str, Any] = {
        "boring_streak": boring_streak,
        "drama_weight": drama_weight,
        "fired": fired,
        "selected_seed_id": selected_seed_id,
        "reason": reason,
        **attrs,
    }
    with Span.open(SPAN_LULL_ESCALATION, attributes, tracer_override=_tracer) as span:
        yield span
