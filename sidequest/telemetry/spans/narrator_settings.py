"""OTEL span for the per-turn active narrator verbosity + vocabulary
(Story 82-2, ADR-049).

``_build_turn_context`` resolves the player-chosen verbosity/vocabulary (or the
``default_for_player_count`` fallback when the session carries no choice) and
stamps them onto the ``TurnContext`` so the ``[NARRATION LENGTH]`` /
``[NARRATION VOCABULARY]`` prompt sections render the right variant. This span
is the GM-panel lie detector for that decision: it records the *active* setting
the narrator received and whether each axis came from the player's explicit
choice or the count-based default — so the dev can confirm the slider is wired
through, not improvised. Routed under the ``narrator`` component to sit
alongside the other narrator-prompt signals on the panel.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_NARRATOR_SETTINGS = "narrator.settings_resolved"
SPAN_ROUTES[SPAN_NARRATOR_SETTINGS] = SpanRoute(
    event_type="state_transition",
    component="narrator",
    extract=lambda span: {
        "field": "narrator_settings",
        "op": "settings_resolved",
        "narrator_verbosity": (span.attributes or {}).get("narrator_verbosity", ""),
        "narrator_vocabulary": (span.attributes or {}).get("narrator_vocabulary", ""),
        "verbosity_source": (span.attributes or {}).get("verbosity_source", ""),
        "vocabulary_source": (span.attributes or {}).get("vocabulary_source", ""),
    },
)


@contextmanager
def narrator_settings_span(
    *,
    narrator_verbosity: str,
    narrator_vocabulary: str,
    verbosity_source: str,
    vocabulary_source: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 82-2: emitted once per ``_build_turn_context`` recording the active
    narrator verbosity + vocabulary the turn will render with.

    ``*_source`` is ``"player"`` when the session carried an explicit choice or
    ``"default_for_player_count"`` when the count-based fallback supplied it —
    so the GM panel can tell a real player setting from the default.
    """
    attributes: dict[str, Any] = {
        "narrator_verbosity": narrator_verbosity,
        "narrator_vocabulary": narrator_vocabulary,
        "verbosity_source": verbosity_source,
        "vocabulary_source": vocabulary_source,
        **attrs,
    }
    with Span.open(SPAN_NARRATOR_SETTINGS, attributes, tracer_override=_tracer) as span:
        yield span
