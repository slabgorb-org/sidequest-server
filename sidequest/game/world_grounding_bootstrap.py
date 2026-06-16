"""Story 24-10 — session-bootstrap assembly of world-grounding state.

This module owns the (zone, season, seed) SELECTION decision and the
one-shot weather sample, keeping that policy OUT of the pure YAML loaders
in :mod:`sidequest.game.world_grounding_loader` (story-context guardrail:
"the seed-pick ... somewhere visible ... NOT inline in the loader").

Scope (24-10): a single bootstrap-time WeatherState per session, plus the
two authored YAML dicts loaded verbatim. Per-turn weather re-roll,
calendar-driven seasonal advancement, and location-graph zone selection
are explicitly OUT of scope — future stories.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sidequest.game.weather import ClimateRulesFile, WeatherGenerator, WeatherState
from sidequest.game.world_grounding_loader import (
    load_pack_weather,
    load_world_calendar,
    load_world_demographics,
)

__all__ = ["WorldGroundingBootstrap", "load_world_grounding"]

# Per-genre bootstrap (zone, season) selection — the visible, documented
# playtest hardcode (24-10). tea_and_murder opens in autumn at glen_floor
# (story-context guardrail). A genre not listed here falls back to the
# deterministic default below: the first declared climate zone and the
# first declared season of that zone (authored insertion order). That
# default is a legitimate choice of which palette to sample, NOT a silent
# error-masking fallback — the weather.yaml is present and validated; we
# are only picking the opening (zone, season).
_BOOTSTRAP_SELECTION: dict[str, tuple[str, str]] = {
    "tea_and_murder": ("glen_floor", "autumn"),
}


@dataclass(frozen=True)
class WorldGroundingBootstrap:
    """The three world-grounding values assembled at session bootstrap.

    Any field may be ``None`` — that is the legitimate "pack/world authored
    no grounding for this surface" state, NOT an error.
    """

    weather_state: WeatherState | None
    demographics: dict[str, Any] | None
    calendar: dict[str, Any] | None


def _select_zone_season(rules: ClimateRulesFile, genre_slug: str) -> tuple[str, str]:
    """Pick the (zone, season) to sample at bootstrap.

    Genre override first (documented playtest choice), validated against the
    loaded rules so a stale override — e.g. a zone renamed in the YAML —
    fails loud rather than silently drifting to a default. Otherwise the
    deterministic default: first zone, first season of that zone.
    """
    override = _BOOTSTRAP_SELECTION.get(genre_slug)
    if override is not None:
        zone, season = override
        if zone not in rules.climate_zones:
            raise ValueError(
                f"world-grounding bootstrap selection for genre {genre_slug!r} "
                f"names zone {zone!r}, which is not in the loaded weather.yaml "
                f"(zones: {sorted(rules.climate_zones)}). The hardcoded "
                f"selection is stale — fix _BOOTSTRAP_SELECTION or the YAML."
            )
        if season not in rules.climate_zones[zone].seasons:
            raise ValueError(
                f"world-grounding bootstrap selection for genre {genre_slug!r} "
                f"names season {season!r} in zone {zone!r}, which is not "
                f"defined (seasons: {sorted(rules.climate_zones[zone].seasons)})."
            )
        return zone, season
    zone = next(iter(rules.climate_zones))
    season = next(iter(rules.climate_zones[zone].seasons))
    return zone, season


def load_world_grounding(
    *,
    world_dir: Path | str,
    genre_slug: str,
    seed_source: str,
) -> WorldGroundingBootstrap:
    """Assemble the session's world-grounding state at connect time.

    ``seed_source`` (the session's game_slug) is hashed to a stable seed via
    ``zlib.crc32`` so the bootstrap WeatherState is deterministic per session
    and reproducible across process restarts (Python's builtin ``hash`` is
    salted per-process and would not be).

    Raises (No Silent Fallbacks) when any present grounding file is malformed
    or violates its schema — the connect handler surfaces this as a typed
    error. A pack/world that authored no grounding yields all-``None`` (the
    loaders return None for absent files) — clean, not an error.

    Note: weather is read from ``world_dir/weather.yaml`` (epic 74 — weather is
    world-tier flavor; the pack root is no longer consulted). ``load_pack_weather``
    parses + validates the climate YAML to give us the present/absent/malformed
    trichotomy and an early loud failure; ``WeatherGenerator`` then re-reads the
    same small file (its constructor only accepts a path — weather.py rework is
    out of scope for epic 74). One extra parse of one file, once per session —
    acceptable.
    """
    # Epic 74 — weather is WORLD-tier flavor (climate belongs to the world, not
    # the shared genre). Read world_dir/weather.yaml; the pack-root file is no
    # longer consulted. ``load_pack_weather`` takes any dir and reads its
    # ``weather.yaml``, so repointing it to world_dir is the whole change.
    weather_rules = load_pack_weather(world_dir)
    weather_state: WeatherState | None = None
    if weather_rules is not None:
        zone, season = _select_zone_season(weather_rules, genre_slug)
        seed = zlib.crc32(seed_source.encode("utf-8"))
        generator = WeatherGenerator(Path(world_dir) / "weather.yaml")
        # generate() fires the world_grounding.weather_proposed OTEL span
        # (24-7 hook) — the GM panel's proposed-vs-used lie detector.
        weather_state = generator.generate(zone, season, seed)

    demographics = load_world_demographics(world_dir)
    calendar = load_world_calendar(world_dir)

    return WorldGroundingBootstrap(
        weather_state=weather_state,
        demographics=demographics,
        calendar=calendar,
    )
