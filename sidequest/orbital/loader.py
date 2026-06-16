"""Loader for orbital world content (orbits.yaml + chart.yaml).

Per CLAUDE.md "No Silent Fallbacks" — missing required files for an
`orbital`-tier world raise OrbitalContentMissingError with a clear path.
chart.yaml is optional (renderer falls back to no flavor layer).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from sidequest.orbital.models import ChartConfig, OrbitsConfig


class OrbitalContentMissingError(FileNotFoundError):
    """Raised when an `orbital`-tier world is missing orbits.yaml."""


@dataclass(frozen=True)
class OrbitalContent:
    orbits: OrbitsConfig
    chart: ChartConfig


def load_orbital_content(world_dir: Path) -> OrbitalContent:
    """Load orbits.yaml (+ chart.yaml if present) from `world_dir`.

    Behavior:
      - orbits.yaml present → parsed and validated into OrbitsConfig.
      - orbits.yaml absent → OrbitalContentMissingError (the world is
        opting into orbital semantics by being passed to this loader).
      - chart.yaml present → parsed into ChartConfig.
      - chart.yaml absent → empty ChartConfig (no flavor layer).

    Schema validation errors propagate as pydantic ValidationError with
    enough context to pinpoint the offending body / field.
    """
    world_dir = Path(world_dir)
    orbits_path = world_dir / "orbits.yaml"
    chart_path = world_dir / "chart.yaml"

    if not orbits_path.exists():
        raise OrbitalContentMissingError(
            f"orbits.yaml missing under {world_dir}; required for orbital tier"
        )

    with orbits_path.open() as f:
        orbits_raw = yaml.safe_load(f)
    orbits = OrbitsConfig.model_validate(orbits_raw)

    if chart_path.exists():
        with chart_path.open() as f:
            chart_raw = yaml.safe_load(f)
        chart = ChartConfig.model_validate(chart_raw)
    else:
        chart = ChartConfig(version=orbits.version, annotations=[])

    return OrbitalContent(orbits=orbits, chart=chart)
