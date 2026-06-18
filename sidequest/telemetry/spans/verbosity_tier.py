"""OTEL span for the per-turn drama-aware narrator length cap (Story 126-11,
SOUL "Cost Scales with Drama").

``build_narrator_prompt`` resolves the narrator's hard ``<length-limit>`` cap
from the player's verbosity mode (the per-mode base) scaled by the turn's drama
weight (``TurnContext.pacing_hint.drama_weight``). This span is the GM-panel lie
detector for that decision: it records the chosen tier, the drama weight that
selected it, and the resulting sentence/character cap — so the dev can confirm
the cap actually tracks the turn's drama instead of the narrator improvising a
length. Routed under the ``narrator`` component to sit beside the other
narrator-prompt signals (``narrator.settings_resolved`` / pacing).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_VERBOSITY_TIER = "narrator.verbosity_tier"
SPAN_ROUTES[SPAN_VERBOSITY_TIER] = SpanRoute(
    event_type="state_transition",
    component="narrator",
    extract=lambda span: {
        "field": "verbosity_tier",
        "op": "tier_resolved",
        "tier": (span.attributes or {}).get("tier", ""),
        "weight": (span.attributes or {}).get("weight", 0.0),
        "cap_sentences": (span.attributes or {}).get("cap_sentences", 0),
        "cap_chars": (span.attributes or {}).get("cap_chars", 0),
        "verbosity": (span.attributes or {}).get("verbosity", ""),
    },
)


@contextmanager
def verbosity_tier_span(
    *,
    tier: str,
    weight: float,
    cap_sentences: int,
    cap_chars: int,
    verbosity: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 126-11: emitted once per narrator prompt build recording the
    drama-scaled length cap the turn will render with.

    - ``tier``: the chosen verbosity tier (``baseline`` when no drama signal was
      derivable, else ``quiet`` / ``normal`` / ``climax``)
    - ``weight``: the drama weight (0.0–1.0) that selected the tier
    - ``cap_sentences`` / ``cap_chars``: the hard cap the prompt renders
    - ``verbosity``: the player's verbosity mode the cap scaled around
    """
    attributes: dict[str, Any] = {
        "tier": tier,
        "weight": weight,
        "cap_sentences": cap_sentences,
        "cap_chars": cap_chars,
        "verbosity": verbosity,
        **attrs,
    }
    with Span.open(SPAN_VERBOSITY_TIER, attributes, tracer_override=_tracer) as span:
        yield span
