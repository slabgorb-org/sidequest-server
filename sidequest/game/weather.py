"""Procedural weather generator (Story 24-5, Epic 24).

Loads a pack-level ``weather.yaml`` (schema: docs/schemas/world-grounding/
weather.schema.json), validates it through Pydantic, and samples a typed
``WeatherState`` from a climate zone + season + RNG seed.

The generator is deterministic: ``generate(zone, season, seed)`` returns the
same ``WeatherState`` for identical arguments. Same-seed reproducibility is
what makes the narrator-side state injection auditable (story 24-7 will add
the OTEL span over this same seam).

The Anthropic-SDK narrator path consumes the resulting ``WeatherState`` via
the prompt-zone injection added in story 24-6 — that wiring is out of scope
here. The CLI in ``sidequest.cli.weathergen`` is the present production
consumer.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Climate YAML schema (mirrors docs/schemas/world-grounding/weather.schema.json)
# ---------------------------------------------------------------------------


class SpecialEvent(BaseModel):
    """A rare weather event that overrides the season palette when it fires."""

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
    weights: Annotated[list[int], Field(min_length=1)]
    precipitation_chance: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_weights_match_conditions(self) -> SeasonPalette:
        if len(self.weights) != len(self.conditions):
            raise ValueError(
                f"weights length ({len(self.weights)}) does not match "
                f"conditions length ({len(self.conditions)}) — the generator "
                f"cannot sample without a 1:1 correspondence"
            )
        if any(w < 0 for w in self.weights):
            raise ValueError("weights must be non-negative integers")
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
# Runtime weather state (consumed by narrator prompt-zone injection in 24-6)
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
        self._path = path

    @property
    def climate_rules(self) -> ClimateRulesFile:
        return self._rules

    def generate(self, zone: str, season: str, seed: int) -> WeatherState:
        if zone not in self._rules.climate_zones:
            available = ", ".join(sorted(self._rules.climate_zones))
            raise KeyError(f"unknown weather zone '{zone}' (available: {available})")
        zone_def = self._rules.climate_zones[zone]
        if season not in zone_def.seasons:
            available = ", ".join(sorted(zone_def.seasons))
            raise KeyError(f"unknown season '{season}' in zone '{zone}' (available: {available})")
        palette = zone_def.seasons[season]
        rng = random.Random(seed)

        # 1. Special-event roll — eligible events in declaration order; first to
        #    fire wins. Outside-season events are skipped without consuming RNG
        #    state, keeping condition draws stable across seasons.
        fired_event: SpecialEvent | None = None
        for event in zone_def.special_events:
            if event.season is not None and event.season != season:
                continue
            if rng.random() < event.chance:
                fired_event = event
                break

        # 2. Condition draw from the weighted palette.
        (condition,) = rng.choices(palette.conditions, weights=palette.weights, k=1)

        # 3. Temperature: uniform over [min, max] degrees Celsius.
        lo, hi = palette.temp_range
        temperature_c = rng.uniform(lo, hi)

        # 4. Precipitation: bernoulli over the season's chance. Schema-optional;
        #    when omitted the generator treats as 0.0 (schema permits inference,
        #    we choose the minimal interpretation — no silent climate invention).
        precip_chance = (
            palette.precipitation_chance if palette.precipitation_chance is not None else 0.0
        )
        precipitation = rng.random() < precip_chance

        return WeatherState(
            zone=zone,
            season=season,
            condition=condition,
            temperature_c=temperature_c,
            precipitation=precipitation,
            special_event=fired_event.name if fired_event is not None else None,
            effects=list(fired_event.effects) if fired_event is not None else [],
            seed=seed,
        )
