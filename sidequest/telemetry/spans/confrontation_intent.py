"""Confrontation intent-validator spans.

Spec 2026-05-20 confrontation-intent-validator step 8. Fired by the
validator dispatch in sidequest.server.narration_apply and the reprompt
loop in sidequest.server.websocket_session_handler._execute_narration_turn.

The intent_mismatch span is the ADR-067 inference-site emission promised
in the unified-narrator-agent design. Replaces the legacy prose-regex
lie-detector (deleted 2026-05-20).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_CONFRONTATION_INTENT_MISMATCH = "confrontation.intent_mismatch"
SPAN_ROUTES[SPAN_CONFRONTATION_INTENT_MISMATCH] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.intent_mismatch",
        "matched_type": (span.attributes or {}).get("matched_type", ""),
        "declared_type": (span.attributes or {}).get("declared_type", ""),
        "severity": (span.attributes or {}).get("severity", ""),
        "matched_tokens": (span.attributes or {}).get("matched_tokens", ""),
        "reprompt_attempted": (span.attributes or {}).get("reprompt_attempted", False),
        "outcome": (span.attributes or {}).get("outcome", ""),
    },
)

# Story 59-1 — no-emission lie-detector. The intent_mismatch span above only
# fires when the narrator emitted an ``action_rewrite.intent`` to tokenize.
# The 2026-05-21 Glenross playtest hit the OTHER blind spot: the narrator
# wrote a textbook standoff but emitted NO confrontation field, NO beats,
# AND NO intent — so ``validate()`` returned None and nothing engaged,
# silently. This span covers that case so the GM panel ("lie detector") sees
# a confrontation-shaped turn that produced zero mechanical backing. It is a
# STRUCTURAL signal (no engagement + no intent), not prose keyword-scanning
# (the deleted ``_CONFRONTATION_TRIGGER_PATTERNS`` regex stays dead).
SPAN_CONFRONTATION_UNENGAGED_TURN = "confrontation.unengaged_turn"
SPAN_ROUTES[SPAN_CONFRONTATION_UNENGAGED_TURN] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.unengaged_turn",
        "player_name": (span.attributes or {}).get("player_name", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)


@contextmanager
def confrontation_intent_mismatch_span(
    *,
    matched_type: str,
    declared_type: str | None,
    severity: str,
    matched_tokens: tuple[str, ...],
    reprompt_attempted: bool = False,
    outcome: str | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emitted when the validator returns a ValidationResult."""
    span_attrs: dict[str, Any] = {
        "matched_type": matched_type,
        "declared_type": declared_type or "",
        "severity": severity,
        "matched_tokens": ",".join(matched_tokens),
        "reprompt_attempted": reprompt_attempted,
    }
    if outcome is not None:
        span_attrs["outcome"] = outcome
    span_attrs.update(attrs)
    with Span.open(
        SPAN_CONFRONTATION_INTENT_MISMATCH,
        span_attrs,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def confrontation_unengaged_turn_span(
    *,
    player_name: str,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emitted when a turn engages no confrontation AND emits no intent.

    Story 59-1 no-emission lie-detector. Fires from
    ``sidequest.server.narration_apply`` when, with a pack loaded and no
    active encounter, the narrator set no ``confrontation`` field and no
    ``action_rewrite.intent`` — the validator's blind spot, where it cannot
    even tokenize an intent to flag a mismatch. The GM panel reads this so a
    winged (prose-only) confrontation cannot regress silently.
    """
    span_attrs = {"player_name": player_name, "genre_slug": genre_slug, **attrs}
    with Span.open(
        SPAN_CONFRONTATION_UNENGAGED_TURN,
        span_attrs,
        tracer_override=_tracer,
    ) as span:
        yield span
