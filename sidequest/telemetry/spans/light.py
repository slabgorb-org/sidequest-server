"""light.* survival-clock spans (Light & Darkness §OTEL Contract).

The GM panel is the lie detector: the environment_clock burn must be provably
engaged vs. improvised. ``light.tick`` (INFO) fires once per environment_clock
tick against a real ``light`` pool — both the unlit burn path and the lit
no-burn path — so the panel sees the survival clock turning (or holding) on
every time-advancing turn, never a narrator improvising the dark.

The relight span ``light.relit`` is added in Phase 4.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# ---------------------------------------------------------------------------
# Span name constants
# ---------------------------------------------------------------------------

SPAN_LIGHT_TICK = "light.tick"

# ---------------------------------------------------------------------------
# Routing registration
# ---------------------------------------------------------------------------


def _attr(field: str):
    return lambda span, f=field: (span.attributes or {}).get(f)


SPAN_ROUTES[SPAN_LIGHT_TICK] = SpanRoute(
    event_type="state_transition",
    component="environment_clock",
    extract=lambda s: {
        "field": "resources",
        "op": "light.tick",
        "region": _attr("region")(s),
        "lit": _attr("lit")(s),
        "burned": _attr("burned")(s),
        "light.current": _attr("light.current")(s),
        "light.max": _attr("light.max")(s),
        "crossed_threshold": _attr("crossed_threshold")(s),
        "penalty_applied": _attr("penalty_applied")(s),
    },
)

# ---------------------------------------------------------------------------
# Context-manager helper
# ---------------------------------------------------------------------------


@contextmanager
def light_tick_span(
    *,
    region: str,
    lit: bool,
    burned: bool,
    light_current: float,
    light_max: float,
    crossed_threshold: str = "",
    penalty_applied: bool = False,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``light.tick`` INFO span for one environment_clock tick.

    ``burned`` distinguishes the unlit burn path (True) from the lit no-burn
    path (False); both are real clock decisions and so both emit. ``crossed_threshold``
    is the joined resource-threshold event ids the burn crossed this tick (""
    when none — OTEL attributes cannot be None). ``penalty_applied`` is True
    when this tick newly minted the darkness penalty status."""
    with Span.open(
        SPAN_LIGHT_TICK,
        {
            "region": region,
            "lit": lit,
            "burned": burned,
            "light.current": light_current,
            "light.max": light_max,
            "crossed_threshold": crossed_threshold,
            "penalty_applied": penalty_applied,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


__all__ = [
    "SPAN_LIGHT_TICK",
    "light_tick_span",
]
