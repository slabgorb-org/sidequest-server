"""World-grounding YAML loaders + bootstrap orchestration (Story 24-10).

Epic 24 shipped a complete world-grounding pipeline (weather generator,
demographics/calendar authoring, prompt-zone consumer, OTEL spans) but none
of it was reachable from a running session: no loader read the authored YAML
and the three ``ToolContext`` grounding fields were always ``None``. This
module is the bridge — it reads the authored files off disk and produces the
runtime objects the per-turn ``ToolContext`` carries.

File locations (authority: Story 24-1 schema):

* ``weather.yaml``       — **pack** level: ``<pack_dir>/weather.yaml``
* ``demographics.yaml``  — **world** level: ``<world_dir>/demographics.yaml``
* ``calendar.yaml``      — **world** level: ``<world_dir>/calendar.yaml``

Per CLAUDE.md "No Silent Fallbacks": a file that is *absent* yields ``None``
(a legitimate authoring choice — not every pack grounds weather), but a file
that is *present but malformed* raises loudly. A misfiled or broken grounding
file must surface at bootstrap, never degrade to a silent ``None`` and a
downstream "grounding mysteriously absent" symptom.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sidequest.game.weather import ClimateRulesFile, WeatherGenerator, WeatherState

__all__ = [
    "WorldGrounding",
    "load_pack_weather",
    "load_world_calendar",
    "load_world_demographics",
    "load_world_grounding",
]

_WEATHER_FILENAME = "weather.yaml"
_DEMOGRAPHICS_FILENAME = "demographics.yaml"
_CALENDAR_FILENAME = "calendar.yaml"


def _load_mapping(path: Path) -> dict[str, Any]:
    """Parse a YAML file that must deserialize to a mapping.

    Raises loudly on parse failure (``yaml.YAMLError``) or on a non-mapping
    top-level document — never returns a partial/empty fallback.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path.name} at {path} did not parse as a mapping (got {type(raw).__name__})"
        )
    return raw


def load_pack_weather(pack_dir: Path) -> ClimateRulesFile | None:
    """Load pack-level ``weather.yaml`` into the validated climate model.

    Returns ``None`` when the file is absent. Raises on malformed YAML or a
    document that violates the :class:`ClimateRulesFile` schema (e.g. a
    weights/conditions length mismatch) — no silent fallback to a default
    palette.
    """
    path = pack_dir / _WEATHER_FILENAME
    if not path.exists():
        return None
    return ClimateRulesFile.model_validate(_load_mapping(path))


def load_world_demographics(world_dir: Path) -> dict[str, Any] | None:
    """Load world-level ``demographics.yaml`` into a plain dict.

    Returns ``None`` when absent, raises on malformed YAML. ``world_dir`` is
    the world directory, not the pack root — a ``demographics.yaml`` misfiled
    at the pack root is intentionally NOT picked up (honours the pack/world
    split; reaching up would be a silent wrong-path fallback).
    """
    path = world_dir / _DEMOGRAPHICS_FILENAME
    if not path.exists():
        return None
    return _load_mapping(path)


def load_world_calendar(world_dir: Path) -> dict[str, Any] | None:
    """Load world-level ``calendar.yaml`` into a plain dict.

    Returns ``None`` when absent, raises on malformed YAML.
    """
    path = world_dir / _CALENDAR_FILENAME
    if not path.exists():
        return None
    return _load_mapping(path)


@dataclass(frozen=True, slots=True)
class WorldGrounding:
    """Bootstrap-time grounding bundle stamped onto the session carrier.

    Each field is ``None`` when the pack/world did not author that file.
    """

    weather_state: WeatherState | None
    world_demographics: dict[str, Any] | None
    world_calendar: dict[str, Any] | None


def load_world_grounding(
    *,
    pack_dir: Path,
    world_dir: Path,
    zone: str,
    season: str,
    seed: int,
) -> WorldGrounding:
    """Connect-time orchestration: load all three grounding sections.

    When a pack-level ``weather.yaml`` is present, constructs a single
    :class:`WeatherGenerator` and samples one :class:`WeatherState` for the
    given ``(zone, season, seed)`` — that ``generate()`` call fires the
    Story 24-7 ``world_grounding.weather_proposed`` span (the GM-panel proof
    that bootstrap actually sampled weather, not just held inert YAML). When
    no ``weather.yaml`` is authored, ``weather_state`` stays ``None`` and the
    generator is never constructed (no proposed span).

    Demographics and calendar load independently of weather.
    """
    weather_path = pack_dir / _WEATHER_FILENAME
    weather_state: WeatherState | None = None
    if weather_path.exists():
        generator = WeatherGenerator(weather_path)
        weather_state = generator.generate(zone=zone, season=season, seed=seed)

    return WorldGrounding(
        weather_state=weather_state,
        world_demographics=load_world_demographics(world_dir),
        world_calendar=load_world_calendar(world_dir),
    )
