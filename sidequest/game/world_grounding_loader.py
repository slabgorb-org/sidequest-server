"""World-grounding YAML loaders (Story 24-10, Epic 24).

Epic 24 authored the world-grounding content (weather rules, demographics,
calendar) and the narrator-facing :func:`get_world_grounding` tool, but
nothing in production ever read the YAML or populated the
:class:`~sidequest.agents.tool_registry.ToolContext` fields. This module is
the bootstrap-time read side: three small loaders the session-connect path
calls once per session.

Discovery is BY FILE PRESENCE, not by a ``pack.yaml`` flag — authoring the
YAML IS the declaration (symmetric with how ``cultures.yaml`` is
discovered). A pack/world that never authored a surface simply has no file,
and the loader returns ``None`` (legitimate absence). A file that exists
but is malformed raises loudly per CLAUDE.md "No Silent Fallbacks": a silent
``None`` there would surface three turns into a session as a baffling
"weather grounding mysteriously absent" symptom with nothing in the GM
panel to explain it.

File placement (load-bearing — honour the pack-vs-world split from 24-1):

  * ``weather.yaml``      — PACK level   (``<pack>/weather.yaml``)
  * ``demographics.yaml`` — WORLD level  (``<pack>/worlds/<world>/demographics.yaml``)
  * ``calendar.yaml``     — WORLD level  (``<pack>/worlds/<world>/calendar.yaml``)

The weather loader returns the already-validated
:class:`~sidequest.game.weather.ClimateRulesFile` (NOT a raw dict) so
per-zone / per-season schema violations surface here, at session bootstrap,
rather than deep inside a per-turn ``WeatherGenerator.generate()`` call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sidequest.game.weather import ClimateRulesFile

__all__ = [
    "load_pack_weather",
    "load_world_calendar",
    "load_world_demographics",
]


def load_pack_weather(pack_dir: Path | str) -> ClimateRulesFile | None:
    """Load and validate ``<pack_dir>/weather.yaml``.

    Returns the validated :class:`ClimateRulesFile` when the file exists,
    or ``None`` when it is absent (the pack declared no weather grounding —
    legitimate). Raises when the file exists but is malformed YAML or
    violates the climate schema (No Silent Fallbacks).
    """
    path = Path(pack_dir) / "weather.yaml"
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"weather.yaml at {path} did not parse as a mapping (got {type(raw).__name__})"
        )
    # model_validate raises pydantic.ValidationError on schema violation —
    # surfaced loud at bootstrap, exactly the intent of AC8.
    return ClimateRulesFile.model_validate(raw)


def load_world_demographics(world_dir: Path | str) -> dict[str, Any] | None:
    """Load ``<world_dir>/demographics.yaml`` as a plain dict.

    World-level surface — looks ONLY in ``world_dir``. A misfiled pack-root
    ``demographics.yaml`` is invisible here by design (no walk-up fallback).
    Returns ``None`` when absent; raises when malformed.
    """
    return _load_world_mapping(world_dir, "demographics.yaml")


def load_world_calendar(world_dir: Path | str) -> dict[str, Any] | None:
    """Load ``<world_dir>/calendar.yaml`` as a plain dict.

    World-level surface — same absence/malformed semantics as
    :func:`load_world_demographics`.
    """
    return _load_world_mapping(world_dir, "calendar.yaml")


def _load_world_mapping(world_dir: Path | str, filename: str) -> dict[str, Any] | None:
    """Shared world-level YAML reader: absent → None, malformed → raise,
    present-but-not-a-mapping → raise. Demographics and calendar are both
    authored verbatim (no procedural generation), so a plain dict is the
    contract — the narrator tool surfaces them as-is."""
    path = Path(world_dir) / filename
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"{filename} at {path} did not parse as a mapping (got {type(raw).__name__})"
        )
    return raw
