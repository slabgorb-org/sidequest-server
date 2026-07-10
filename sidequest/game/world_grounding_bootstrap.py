"""Story 24-10 — session-bootstrap assembly of world-grounding state.

This module owns the (zone, season, seed) SELECTION decision and the
one-shot weather sample, keeping that policy OUT of the pure YAML loaders
in :mod:`sidequest.game.world_grounding_loader` (story-context guardrail:
"the seed-pick ... somewhere visible ... NOT inline in the loader").

Scope (24-10 + 163-6 spec §2 A2): a bootstrap-time WeatherState per session,
now geography-aware — the starting region's ``weather_zone`` selects the zone
(``_select_zone_for_region``) — plus the two authored YAML dicts loaded
verbatim, and per-region-change re-sampling (``regenerate_weather_for_region``).
Mid-scene per-turn weather re-roll and calendar-driven seasonal advancement
remain OUT of scope — future stories.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sidequest.game.weather import ClimateRulesFile, WeatherGenerator, WeatherState
from sidequest.game.world_grounding_loader import (
    load_pack_weather,
    load_world_calendar,
    load_world_demographics,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.genre.models.world import CartographyConfig

__all__ = [
    "WorldGroundingBootstrap",
    "load_world_grounding",
    "regenerate_weather_for_region",
]

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
    """The world-grounding values assembled at session bootstrap.

    ``weather_state`` + the ``weather_generator``/``weather_season`` cache (the
    latter two exist only to support post-bootstrap per-region re-sampling, not
    as independent grounding surfaces), plus ``demographics`` and ``calendar``.
    Any field may be ``None`` — that is the legitimate "pack/world authored no
    grounding for this surface" state, NOT an error.
    """

    weather_state: WeatherState | None
    demographics: dict[str, Any] | None
    calendar: dict[str, Any] | None
    # Spec §2 A2: the generator + selected season are cached on the session so
    # per-region-change re-sampling (regenerate_weather_for_region) needs no
    # world-dir resolution on the hot turn path. Both None when the world
    # authored no weather.yaml.
    weather_generator: WeatherGenerator | None = None
    weather_season: str | None = None


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


def _select_zone_for_region(rules: ClimateRulesFile, cartography: Any, genre_slug: str) -> str:
    """Spec §2 A2: bind weather to geography at bootstrap.

    Prefer the STARTING region's ``weather_zone`` over the genre hardcode; a
    declared-but-unknown region zone fails loud (No Silent Fallbacks), mirroring
    ``_select_zone_season``'s stale-override check. Absent a region zone (or any
    cartography), defer to the existing genre-override / first-zone selection.
    """
    start = getattr(cartography, "starting_region", None) if cartography else None
    regions = getattr(cartography, "regions", {}) if cartography else {}
    region = regions.get(start) if isinstance(regions, dict) else None
    wz = getattr(region, "weather_zone", None) if region is not None else None
    if wz is not None:
        if wz not in rules.climate_zones:
            raise ValueError(
                f"region {start!r} declares weather_zone {wz!r}, not in weather.yaml "
                f"(zones: {sorted(rules.climate_zones)})"
            )
        # OTEL (spec §2 A2): record WHICH strategy chose the bootstrap zone so
        # the GM panel can verify the region override fired rather than silently
        # falling back — the bootstrap sibling of the region-change
        # ``weather.zone_changed`` span.
        _watcher_publish(
            "weather.bootstrap_zone_selected",
            {"zone": wz, "strategy": "region", "region": start or ""},
            component="location",
        )
        return wz
    zone, _season = _select_zone_season(rules, genre_slug)
    _watcher_publish(
        "weather.bootstrap_zone_selected",
        {"zone": zone, "strategy": "genre_default", "region": start or ""},
        component="location",
    )
    return zone


def regenerate_weather_for_region(sd: Any, region_id: str, zone: str) -> None:
    """Spec §2 A2: re-sample ``sd.weather_state`` for a newly-entered region.

    The seed is derived from the region (``crc32(game_slug:region_id)``), so the
    same region always yields the same weather — reproducible across restarts,
    auditable by the GM panel. No-ops when the world authored no weather (the
    generator is None) — never substitutes default weather silently.

    ``sd`` is typed ``Any`` to avoid a ``sidequest.game`` → ``sidequest.server``
    import cycle (it is a ``_SessionData``); it must expose ``weather_generator``,
    ``weather_season``, ``game_slug``, and a writable ``weather_state``.
    """
    if sd.weather_generator is None:
        return
    if sd.game_slug is None:
        # No Silent Fallbacks: the seed needs a stable session id. regenerate is
        # only called on the active-session region-change path where game_slug is
        # always set — a None here is a broken invariant, not normal content, so
        # refuse rather than silently seed from the literal "None:<region>".
        raise ValueError(
            "regenerate_weather_for_region requires sd.game_slug for a stable "
            f"per-region seed, but it is None (region={region_id!r})"
        )
    seed = zlib.crc32(f"{sd.game_slug}:{region_id}".encode())
    sd.weather_state = sd.weather_generator.generate(zone, sd.weather_season, seed)


def load_world_grounding(
    *,
    world_dir: Path | str,
    genre_slug: str,
    seed_source: str,
    cartography: CartographyConfig | None = None,
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
    generator: WeatherGenerator | None = None
    season: str | None = None
    if weather_rules is not None:
        # Spec §2 A2: geography drives the zone (starting region's weather_zone
        # wins), but the season binding stays with _select_zone_season — season
        # is out of A2 scope.
        _, season = _select_zone_season(weather_rules, genre_slug)
        zone = _select_zone_for_region(weather_rules, cartography, genre_slug)
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
        weather_generator=generator,
        weather_season=season,
    )
