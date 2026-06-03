"""world_grounding.* OTEL spans — weather + demographics observability (Story 24-7).

Three state_transition spans close the lie-detector loop the GM panel
needs for story 24-8 playtest validation per CLAUDE.md's OTEL
Observability Principle:

* ``world_grounding.weather_proposed`` — fires inside
  :meth:`sidequest.game.weather.WeatherGenerator.generate` every time
  a :class:`~sidequest.game.weather.WeatherState` is sampled. Records
  the full state so the dashboard can render *what the generator
  produced*, regardless of whether the narrator ever asks for it.

* ``world_grounding.weather_used`` — fires inside the
  ``get_world_grounding`` tool handler when the narrator's tool call
  actually returns weather data to the model (``"weather"`` in
  ``include`` AND ``ctx.weather_state`` is not None). Pairs with
  ``weather_proposed`` via the shared ``seed`` join key.

* ``world_grounding.demographics_injected`` — fires inside the
  ``get_world_grounding`` tool handler when the demographics section
  is actually returned (``"demographics"`` in ``include`` AND
  ``ctx.world_demographics`` is not None). One-sided signal —
  demographics is authored YAML, not procedurally generated, so there
  is no "proposed" counterpart.

Encoding contract
~~~~~~~~~~~~~~~~~
Per CLAUDE.md "no silent fallbacks": absent fields are encoded
*explicitly* (empty string for missing event/perspective, ``0`` for
absent count), never omitted from the attribute map. The dashboard's
column-presence check is reliable only when every routed span carries
every documented attribute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

if TYPE_CHECKING:
    from sidequest.game.weather import WeatherState

SPAN_WORLD_GROUNDING_WEATHER_PROPOSED = "world_grounding.weather_proposed"
SPAN_WORLD_GROUNDING_WEATHER_USED = "world_grounding.weather_used"
SPAN_WORLD_GROUNDING_WEATHER_ABSENT = "world_grounding.weather_absent"
SPAN_WORLD_GROUNDING_DEMOGRAPHICS_INJECTED = "world_grounding.demographics_injected"


# ---------------------------------------------------------------------------
# Route extractors — surface the attrs the GM dashboard renders
# ---------------------------------------------------------------------------

SPAN_ROUTES[SPAN_WORLD_GROUNDING_WEATHER_PROPOSED] = SpanRoute(
    event_type="state_transition",
    component="world_grounding",
    extract=lambda span: {
        "field": "weather",
        "op": "proposed",
        "zone": (span.attributes or {}).get("zone", ""),
        "season": (span.attributes or {}).get("season", ""),
        "condition": (span.attributes or {}).get("condition", ""),
        "temperature_c": (span.attributes or {}).get("temperature_c", 0.0),
        "precipitation": (span.attributes or {}).get("precipitation", False),
        "special_event": (span.attributes or {}).get("special_event", ""),
        "effects": (span.attributes or {}).get("effects", ""),
        "seed": (span.attributes or {}).get("seed", 0),
    },
)

SPAN_ROUTES[SPAN_WORLD_GROUNDING_WEATHER_USED] = SpanRoute(
    event_type="state_transition",
    component="world_grounding",
    extract=lambda span: {
        "field": "weather",
        "op": "used",
        "zone": (span.attributes or {}).get("zone", ""),
        "season": (span.attributes or {}).get("season", ""),
        "condition": (span.attributes or {}).get("condition", ""),
        "seed": (span.attributes or {}).get("seed", 0),
        "world_id": (span.attributes or {}).get("world_id", ""),
        "perspective_pc": (span.attributes or {}).get("perspective_pc", ""),
    },
)

SPAN_ROUTES[SPAN_WORLD_GROUNDING_WEATHER_ABSENT] = SpanRoute(
    event_type="state_transition",
    component="world_grounding",
    extract=lambda span: {
        "field": "weather",
        "op": "absent",
        "world_id": (span.attributes or {}).get("world_id", ""),
        "perspective_pc": (span.attributes or {}).get("perspective_pc", ""),
    },
)

SPAN_ROUTES[SPAN_WORLD_GROUNDING_DEMOGRAPHICS_INJECTED] = SpanRoute(
    event_type="state_transition",
    component="world_grounding",
    extract=lambda span: {
        "field": "demographics",
        "op": "injected",
        "world_id": (span.attributes or {}).get("world_id", ""),
        "total_population": (span.attributes or {}).get("total_population", 0),
        "recurring_cast_count": (span.attributes or {}).get("recurring_cast_count", 0),
        "perspective_pc": (span.attributes or {}).get("perspective_pc", ""),
    },
)


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------


def emit_weather_proposed_span(state: WeatherState) -> None:
    """Emit a ``world_grounding.weather_proposed`` span for a sampled state.

    Called from :meth:`WeatherGenerator.generate` after the state is
    constructed and before it is returned. Records every field so the
    dashboard's "proposed vs used" view can diff the proposed row
    against the matching ``weather_used`` row on the shared ``seed``.
    """
    with Span.open(
        SPAN_WORLD_GROUNDING_WEATHER_PROPOSED,
        attrs={
            "zone": state.zone,
            "season": state.season,
            "condition": state.condition,
            "temperature_c": float(state.temperature_c),
            "precipitation": bool(state.precipitation),
            "special_event": state.special_event or "",
            "effects": ",".join(state.effects),
            "seed": int(state.seed),
        },
    ):
        pass


def emit_weather_used_span(
    *,
    zone: str,
    season: str,
    condition: str,
    seed: int,
    world_id: str,
    perspective_pc: str | None,
) -> None:
    """Emit a ``world_grounding.weather_used`` span when weather is
    delivered to the narrator via the grounding tool."""
    with Span.open(
        SPAN_WORLD_GROUNDING_WEATHER_USED,
        attrs={
            "zone": zone,
            "season": season,
            "condition": condition,
            "seed": int(seed),
            "world_id": world_id,
            "perspective_pc": perspective_pc or "",
        },
    ):
        pass


def emit_weather_absent_span(*, world_id: str, perspective_pc: str | None) -> None:
    """Emit a ``world_grounding.weather_absent`` span.

    Fires from the ``get_world_grounding`` tool handler when the narrator's
    ``include`` list contains ``"weather"`` but the session-bound
    ``WeatherState`` is ``None`` — i.e. weather was requested but the world has
    none wired. Companion to ``weather_used``: exactly one of the two fires per
    weather-requested grounding call. Lets the GM panel distinguish "this world
    has no weather **by design**" from "the weather subsystem broke" (CLAUDE.md
    OTEL Observability Principle).

    There is no ``WeatherState`` to record — that is the point — so the span is
    identity-only: which world, whose viewpoint. Per "no silent fallbacks" an
    absent ``perspective_pc`` (single-player) is encoded as ``""``, never
    omitted, so the dashboard's column-presence check stays reliable.
    """
    with Span.open(
        SPAN_WORLD_GROUNDING_WEATHER_ABSENT,
        attrs={
            "world_id": world_id,
            "perspective_pc": perspective_pc or "",
        },
    ):
        pass


def emit_demographics_injected_span(
    *,
    world_id: str,
    demographics: dict[str, Any],
    perspective_pc: str | None,
) -> None:
    """Emit a ``world_grounding.demographics_injected`` span when the
    demographics section is delivered to the narrator.

    Coarse-fingerprint encoding: total parish population (or ``0`` if
    the YAML omits it) and the recurring-cast count. Per the
    "no silent fallbacks" contract, missing fields encode as ``0`` /
    ``""`` rather than being dropped from the attribute map.
    """
    parish = demographics.get("parish") or {}
    total_population = int(parish.get("total_population", 0) or 0)
    cast = demographics.get("recurring_cast") or []
    recurring_cast_count = len(cast)

    with Span.open(
        SPAN_WORLD_GROUNDING_DEMOGRAPHICS_INJECTED,
        attrs={
            "world_id": world_id,
            "total_population": total_population,
            "recurring_cast_count": recurring_cast_count,
            "perspective_pc": perspective_pc or "",
        },
    ):
        pass
