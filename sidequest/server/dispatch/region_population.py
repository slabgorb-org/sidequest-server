"""Read side of the procedural region population (Task 4, ADR-106 / ADR-059).

The materializer freezes each generated region's curated roster as a
``region_population`` dungeon mutation (Task 3). This module reads it back by
region id for the Monster-Manual inject seam — decoupled from the heavy
``materializer.CuratedCreature`` import so the per-turn inject path stays light.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegionCreature:
    """One frozen procedural creature, parsed from the region_population payload."""

    name: str
    creature_type: str
    telegraph: str
    hp: int
    threat_level: int


def _parse(d: dict[str, Any]) -> RegionCreature:
    hp = d["hp"]
    return RegionCreature(
        name=str(d["name"]),
        creature_type=str(d.get("creature_type", "")),
        telegraph=str(d.get("telegraph", "")),
        hp=int(hp["max"] if isinstance(hp, dict) else hp),
        threat_level=int(d["threat_level"]),
    )


def load_region_population(
    dungeon_repository: Any, region_id: str
) -> tuple[list[RegionCreature], RegionCreature | None]:
    """Return ``(roster, big_bad)`` for ``region_id``. Empty roster + None for a
    region with no frozen population (a region materialized before this feature,
    or a non-procedural world) — an absent binding, not a silent fallback."""
    roster: list[RegionCreature] = []
    big_bad: RegionCreature | None = None
    for m in dungeon_repository.load_mutations():
        if m.kind != "region_population" or m.region_id != region_id:
            continue
        roster = [_parse(c) for c in m.payload.get("creatures", [])]
        bb = m.payload.get("big_bad")
        big_bad = _parse(bb) if bb is not None else None
    return roster, big_bad
