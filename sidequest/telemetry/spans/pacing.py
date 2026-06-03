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
