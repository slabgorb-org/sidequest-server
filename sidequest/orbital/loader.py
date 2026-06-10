"""Loader for orbital world content (per-system orbits + chart.yaml).

Per CLAUDE.md "No Silent Fallbacks" — a missing required file for an
`orbital`-tier world raises OrbitalContentMissingError with a clear path.
chart.yaml is optional (renderer falls back to no flavor layer).

Two-scale spatial model (ADR-141, Story 98-2). A world is resolved by layout:

  - **Multi-system** — has a ``systems/`` directory. The per-region file
    ``systems/<region_id>.yaml`` is loaded for the party's current region
    (each file is a single-rooted ``OrbitsConfig`` so ``Scope.system_root()``
    holds verbatim). A missing ``systems/<region_id>.yaml`` fails loud naming
    the path — it must NOT fall back to a stray top-level ``orbits.yaml`` or a
    cluster-wide chart. Every resolution emits an ``orbital.system_resolve``
    OTEL span (region → file → hit/miss).
  - **Single-system** — no ``systems/`` directory, only ``orbits.yaml``. The
    lone orrery loads and the two scales collapse cleanly; ``region_id`` is not
    required (the ``coyote_star`` shape).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from sidequest.orbital.models import ChartConfig, OrbitsConfig
from sidequest.telemetry.spans.system_resolve import emit_system_resolve


class OrbitalContentMissingError(FileNotFoundError):
    """Raised when an `orbital`-tier world is missing its required orbits file."""


@dataclass(frozen=True)
class OrbitalContent:
    orbits: OrbitsConfig
    chart: ChartConfig


def load_orbital_content(world_dir: Path, region_id: str | None = None) -> OrbitalContent:
    """Load the orbital tier for `world_dir`, resolved per the party's region.

    Multi-system world (a ``systems/`` directory exists):
      - ``region_id`` selects ``world_dir/systems/<region_id>.yaml``.
      - The resolution emits an ``orbital.system_resolve`` span (region, file,
        hit/miss).
      - A missing ``systems/<region_id>.yaml`` — or a blank ``region_id`` —
        raises OrbitalContentMissingError naming the path (No Silent Fallbacks).
        A stray top-level ``orbits.yaml`` retirement stub does NOT satisfy
        resolution.

    Single-system world (no ``systems/`` directory):
      - Loads ``world_dir/orbits.yaml``; ``region_id`` is ignored (the two
        scales collapse). Missing ``orbits.yaml`` raises OrbitalContentMissingError.

    chart.yaml (optional, world-level) layers flavor in either case.

    Schema validation errors propagate as pydantic ValidationError with
    enough context to pinpoint the offending body / field.
    """
    world_dir = Path(world_dir)
    systems_dir = world_dir / "systems"

    if systems_dir.is_dir():
        orbits_path = _resolve_system_file(world_dir, systems_dir, region_id)
    else:
        # Single-system world: the lone orrery, two scales collapsed.
        orbits_path = world_dir / "orbits.yaml"
        if not orbits_path.exists():
            raise OrbitalContentMissingError(
                f"orbits.yaml missing under {world_dir}; required for orbital tier"
            )

    with orbits_path.open(encoding="utf-8") as f:
        orbits_raw = yaml.safe_load(f)
    orbits = OrbitsConfig.model_validate(orbits_raw)

    chart_path = world_dir / "chart.yaml"
    if chart_path.exists():
        with chart_path.open(encoding="utf-8") as f:
            chart_raw = yaml.safe_load(f)
        chart = ChartConfig.model_validate(chart_raw)
    else:
        chart = ChartConfig(version=orbits.version, annotations=[])

    return OrbitalContent(orbits=orbits, chart=chart)


def _resolve_system_file(world_dir: Path, systems_dir: Path, region_id: str | None) -> Path:
    """Resolve ``systems/<region_id>.yaml`` for a multi-system world, fail loud.

    Emits ``orbital.system_resolve`` (region, file, hit) so the GM panel can
    verify per-region resolution fired. A blank region or a missing file raises
    OrbitalContentMissingError naming the path — never a fallback to a stray
    top-level ``orbits.yaml`` stub or a cluster-wide chart.
    """
    if not region_id:
        raise OrbitalContentMissingError(
            f"multi-system world {world_dir} requires a region id to resolve "
            "systems/<region_id>.yaml; got a blank region (No Silent Fallbacks)"
        )

    # A region id is a single slug, never a path. Reject path-like ids — path
    # separators, a ``..`` parent ref, or a NUL byte — BEFORE building or
    # probing a path, so a (narrator-influenceable) ``current_region`` can never
    # traverse outside ``systems/`` (CWE-22). Fail loud: a path-like region is
    # invalid input, not a missing file — and a ValueError (unlike
    # OrbitalContentMissingError) is not swallowed by the optional-tier catch in
    # SessionRoom.bind_world, so the bad input surfaces instead of silently
    # yielding no chart.
    if "/" in region_id or "\\" in region_id or "\x00" in region_id or ".." in region_id:
        raise ValueError(
            f"region id {region_id!r} is not a valid system identifier: it may "
            "not contain a path separator, '..', or NUL byte "
            f"(refusing to resolve outside {systems_dir} — No Silent Fallbacks)"
        )

    system_path = systems_dir / f"{region_id}.yaml"
    hit = system_path.exists()
    # Span fires on hit AND miss, before any raise — the miss is observable.
    emit_system_resolve(
        region_id=region_id,
        system_file=f"systems/{region_id}.yaml",
        hit=hit,
    )
    if not hit:
        raise OrbitalContentMissingError(
            f"systems/{region_id}.yaml missing under {world_dir}; the galactic "
            "graph still renders this node, but its orrery is unauthored — "
            "refusing to fall back to a cluster chart (No Silent Fallbacks)"
        )
    return system_path
