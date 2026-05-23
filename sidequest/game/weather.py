"""Procedural weather generator (Story 24-5, Epic 24).

Loads a pack-level ``weather.yaml`` (schema: docs/schemas/world-grounding/
weather.schema.json), validates it through Pydantic, and samples a typed
``WeatherState`` from a climate zone + season + RNG seed.

The generator is deterministic: ``generate(zone, season, seed)`` returns the
same ``WeatherState`` for identical arguments. Same-seed reproducibility is
what makes the narrator-side state injection auditable. Every ``generate()``
call emits a ``world_grounding.weather_proposed`` OTEL span (Story 24-7,
see :mod:`sidequest.telemetry.spans.world_grounding`) so the GM dashboard
can diff the proposed state against the ``world_grounding.weather_used``
span fired by the ``get_world_grounding`` tool — that pair is the
"narrator improvised weather" lie detector.

Narrator-side wiring goes through the ``get_world_grounding`` tool call
(Story 24-6, ADR-102/103). The CLI in ``sidequest.cli.weathergen`` is the
standalone production consumer.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field, model_validator

__all__ = [
    "ClimateRulesFile",
    "ClimateZone",
    "SeasonPalette",
    "SpecialEvent",
    "UnknownWeatherSeason",
    "UnknownWeatherZone",
    "WeatherGenerator",
    "WeatherState",
]


# ---------------------------------------------------------------------------
# Typed exceptions — let the CLI present clean errors without string parsing
# ---------------------------------------------------------------------------


class UnknownWeatherZone(KeyError):
    """Raised when ``generate(zone=...)`` is called with a zone the loaded
    climate rules do not define. Subclasses ``KeyError`` so callers that already
    catch ``KeyError`` on dict-lookup-style errors keep working, but the
    structured ``zone`` / ``available`` attributes let the CLI surface a clean
    message without parsing ``e.args[0]``."""

    def __init__(self, zone: str, available: tuple[str, ...]) -> None:
        self.zone = zone
        self.available = available
        super().__init__(f"unknown weather zone '{zone}' (available: {', '.join(available)})")

    def __str__(self) -> str:
        return self.args[0]


class UnknownWeatherSeason(KeyError):
    """Raised when ``generate(season=...)`` is called with a season not defined
    in the requested zone."""

    def __init__(self, season: str, zone: str, available: tuple[str, ...]) -> None:
        self.season = season
        self.zone = zone
        self.available = available
        super().__init__(
            f"unknown season '{season}' in zone '{zone}' (available: {', '.join(available)})"
        )

    def __str__(self) -> str:
        return self.args[0]


# ---------------------------------------------------------------------------
# Climate YAML schema (mirrors docs/schemas/world-grounding/weather.schema.json)
# ---------------------------------------------------------------------------


class SpecialEvent(BaseModel):
    """A rare weather event that overrides the season palette when it fires.

    ``duration_days`` is loaded from the authored YAML to satisfy the schema's
    ``extra='forbid'`` contract but is not consumed by this generator — event
    duration tracking belongs to a downstream consumer (the narrator agent
    will manage day-by-day persistence as part of the wider world-grounding
    epic). Removing the field here would silently reject the real authored
    ``tea_and_murder/weather.yaml``; consumers may read it via the loaded
    model.
    """

    model_config = {"extra": "forbid"}

    name: str
    chance: float = Field(ge=0.0, le=1.0)
    season: str | None = None
    duration_days: Annotated[list[int], Field(min_length=2, max_length=2)] | None = None
    effects: list[str] = Field(default_factory=list)


class SeasonPalette(BaseModel):
    """The weather palette for a single (zone, season) pair."""

    model_config = {"extra": "forbid"}

    temp_range: Annotated[list[float], Field(min_length=2, max_length=2)]
    conditions: Annotated[list[str], Field(min_length=1)]
    weights: Annotated[list[Annotated[int, Field(ge=0)]], Field(min_length=1)]
    precipitation_chance: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_palette_invariants(self) -> SeasonPalette:
        if len(self.weights) != len(self.conditions):
            raise ValueError(
                f"weights length ({len(self.weights)}) does not match "
                f"conditions length ({len(self.conditions)}) — the generator "
                f"cannot sample without a 1:1 correspondence"
            )
        if sum(self.weights) == 0:
            raise ValueError(
                "weights sum to zero — random.choices cannot sample a "
                "weighted draw with no positive weight"
            )
        if self.temp_range[0] > self.temp_range[1]:
            raise ValueError(
                f"temp_range is [{self.temp_range[0]}, {self.temp_range[1]}] — min must be <= max"
            )
        return self


class ClimateZone(BaseModel):
    """A single climate zone (e.g. ``glen_floor``)."""

    model_config = {"extra": "forbid"}

    seasons: Annotated[dict[str, SeasonPalette], Field(min_length=1)]
    special_events: list[SpecialEvent] = Field(default_factory=list)


class ClimateRulesFile(BaseModel):
    """Top-level pack-level ``weather.yaml`` document."""

    model_config = {"extra": "forbid"}

    climate_zones: Annotated[dict[str, ClimateZone], Field(min_length=1)]


# ---------------------------------------------------------------------------
# Runtime weather state (consumed by downstream narrator prompt-zone injection)
# ---------------------------------------------------------------------------


class WeatherState(BaseModel):
    """Procedurally generated per-scene weather state.

    Fields:
      ``zone, season`` — what was sampled from.
      ``condition`` — palette label (e.g. ``smirr``, ``haar``, ``blizzard``).
      ``temperature_c`` — degrees Celsius, uniformly sampled from temp_range.
      ``precipitation`` — boolean roll vs season's ``precipitation_chance``.
      ``special_event`` — name of an overriding event if one fired, else None.
      ``effects`` — mechanical effect ids from the special event (empty otherwise).
      ``seed`` — the RNG seed used to produce this state, for replay/audit.

    Invariant: ``effects`` may be non-empty only when ``special_event`` is
    set. The generator constructs valid pairs; the model-level validator below
    enforces the same contract on any caller deserializing a ``WeatherState``
    from JSON. (A fired event with zero effects is permitted — vibe-only
    events are valid per the authored schema, and pack authors can express
    "narrative-only" beats with ``effects: []``.)
    """

    model_config = {"extra": "forbid"}

    zone: str
    season: str
    condition: str
    temperature_c: float
    precipitation: bool
    special_event: str | None
    effects: list[str]
    seed: int

    @model_validator(mode="after")
    def _validate_event_effects_coupling(self) -> WeatherState:
        if self.effects and self.special_event is None:
            raise ValueError(
                "WeatherState invariant: effects without a special_event is "
                f"incoherent. Got special_event=None, effects={self.effects!r}"
            )
        return self


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class WeatherGenerator:
    """Deterministic weather sampler.

    Construction loads + validates the climate YAML; raises loudly on missing
    file, parse failure, or schema violation.
    """

    def __init__(self, climate_yaml_path: Path | str) -> None:
        path = Path(climate_yaml_path)
        # Per CLAUDE.md "No Silent Fallbacks": the path either exists and parses
        # against the schema, or we raise. Never substitute defaults.
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise ValueError(
                f"weather.yaml at {path} did not parse as a mapping (got {type(raw).__name__})"
            )
        self._rules = ClimateRulesFile.model_validate(raw)

    def generate(self, zone: str, season: str, seed: int) -> WeatherState:
        """Sample a WeatherState for the given (zone, season, seed).

        Same arguments always return an identical WeatherState — the seed
        controls every random draw in the algorithm: special-event eligibility,
        condition weighting, temperature, and precipitation. The CLI exposes
        ``--seed`` for reproducible audit.

        Args:
            zone: Climate zone id (must be a key of ``climate_zones`` in the
                loaded weather.yaml — e.g. ``glen_floor`` in tea_and_murder).
            season: Season id (must be a key of the zone's ``seasons`` map).
            seed: Integer RNG seed. Same value → same output.

        Returns:
            A fully-populated ``WeatherState`` — never ``None``.

        Raises:
            UnknownWeatherZone: ``zone`` is not in the loaded climate rules.
            UnknownWeatherSeason: ``season`` is not in the zone's seasons.
            Both subclass ``KeyError`` for backward-compatible catch blocks.
        """
        if zone not in self._rules.climate_zones:
            raise UnknownWeatherZone(
                zone=zone,
                available=tuple(sorted(self._rules.climate_zones)),
            )
        zone_def = self._rules.climate_zones[zone]
        if season not in zone_def.seasons:
            raise UnknownWeatherSeason(
                season=season,
                zone=zone,
                available=tuple(sorted(zone_def.seasons)),
            )
        palette = zone_def.seasons[season]
        rng = random.Random(seed)

        # 1. Special-event roll. Roll RNG for every event in declaration order
        #    *and then* check season eligibility — this preserves cross-season
        #    RNG state alignment regardless of which events are eligible. First
        #    event to both roll a hit and be in-season wins. Stable invariant:
        #    a given seed consumes exactly len(special_events) random.random()
        #    calls here before condition sampling, no matter which season the
        #    caller requested.
        fired_event: SpecialEvent | None = None
        for event in zone_def.special_events:
            roll = rng.random()
            if event.season is not None and event.season != season:
                continue
            if fired_event is not None:
                continue
            if roll < event.chance:
                fired_event = event

        # 2. Condition draw from the weighted palette.
        (condition,) = rng.choices(palette.conditions, weights=palette.weights, k=1)

        # 3. Temperature: uniform over [min, max] degrees Celsius. Validator
        #    above guarantees min <= max.
        lo, hi = palette.temp_range
        temperature_c = rng.uniform(lo, hi)

        # 4. Precipitation: bernoulli over the season's chance. The schema
        #    marks precipitation_chance optional — when omitted the generator
        #    samples against 0.0 (precipitation never fires). Pack authors
        #    who want rain in a zone must set precipitation_chance explicitly;
        #    the 0.0 fallback is a conservative default, not a derived value
        #    from condition weights.
        precip_chance = (
            palette.precipitation_chance if palette.precipitation_chance is not None else 0.0
        )
        precipitation = rng.random() < precip_chance

        state = WeatherState(
            zone=zone,
            season=season,
            condition=condition,
            temperature_c=temperature_c,
            precipitation=precipitation,
            special_event=fired_event.name if fired_event is not None else None,
            effects=list(fired_event.effects) if fired_event is not None else [],
            seed=seed,
        )
        # Story 24-7: OTEL lie-detector signal for the GM panel. The
        # dashboard pairs this with world_grounding.weather_used (emitted
        # from the grounding tool) to detect narrator-improvised weather.
        from sidequest.telemetry.spans import emit_weather_proposed_span

        emit_weather_proposed_span(state)
        return state
