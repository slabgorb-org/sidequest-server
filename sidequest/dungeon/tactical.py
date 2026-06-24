"""Deterministic tactical-feature derivation for procedural rooms (ADR-096).

Pure functions: given a filled grid (WALL=1/FLOOR=0), the region theme, the
region's neighbours, and its hazard set-pieces, derive the positioned tactical
data the cavern map renders — feature cells, token anchors, POIs, and per-
neighbour exit-threshold cells. Seeded by ``region_id`` so the same region
yields byte-identical output on resume (no ``random``, no clock).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

FLOOR = 0
WALL = 1

# drowned_cavern is the only beneath_sunden theme with flood/drowning motifs — the
# sole water theme as of this audit. Add new water themes here as content grows.
WATER_THEMES: frozenset[str] = frozenset({"drowned_cavern"})

# Fraction of non-choke floor cells the water theme floods; distributed by seeded rotation.
_WATER_FRACTION = 0.18


@dataclass(frozen=True, slots=True)
class TacticalFeatureCell:
    feature_type: str  # cover|hazard|difficult_terrain|water|atmosphere|interactable
    cell: tuple[int, int]
    label: str


@dataclass(frozen=True, slots=True)
class TokenAnchor:
    cell: tuple[int, int]
    role: str  # "entrance" | "creature"


@dataclass(frozen=True, slots=True)
class RegionTactical:
    region_id: str
    features: list[TacticalFeatureCell] = field(default_factory=list)
    anchors: list[TokenAnchor] = field(default_factory=list)
    pois: list[tuple[int, int]] = field(default_factory=list)
    exit_thresholds: dict[str, tuple[int, int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "region_id": self.region_id,
            "features": [
                {"feature_type": f.feature_type, "cell": list(f.cell), "label": f.label}
                for f in self.features
            ],
            "anchors": [{"cell": list(a.cell), "role": a.role} for a in self.anchors],
            "pois": [list(p) for p in self.pois],
            "exit_thresholds": {k: list(v) for k, v in self.exit_thresholds.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> RegionTactical:
        return cls(
            region_id=d["region_id"],
            features=[
                TacticalFeatureCell(f["feature_type"], (f["cell"][0], f["cell"][1]), f["label"])
                for f in d.get("features", [])
            ],
            anchors=[TokenAnchor((a["cell"][0], a["cell"][1]), a["role"]) for a in d.get("anchors", [])],
            pois=[(p[0], p[1]) for p in d.get("pois", [])],
            exit_thresholds={k: (v[0], v[1]) for k, v in d.get("exit_thresholds", {}).items()},
        )


def _seed(region_id: str) -> int:
    """Stable integer seed from the region id (resume-safe; no clock/random)."""
    return int.from_bytes(hashlib.sha256(region_id.encode("utf-8")).digest()[:8], "big")


def _floor_cells(grid: list[list[int]]) -> list[tuple[int, int]]:
    """All FLOOR cells in stable (y, then x) order."""
    cells: list[tuple[int, int]] = []
    for y, row in enumerate(grid):
        for x, v in enumerate(row):
            if v == FLOOR:
                cells.append((x, y))
    return cells


def _floor_neighbors(grid: list[list[int]], x: int, y: int) -> int:
    n = 0
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= ny < len(grid) and 0 <= nx < len(grid[ny]) and grid[ny][nx] == FLOOR:
            n += 1
    return n


def _chokepoints(grid: list[list[int]]) -> list[tuple[int, int]]:
    """Floor cells in a 1-wide passage (<=2 orthogonal floor neighbours)."""
    return [(x, y) for (x, y) in _floor_cells(grid) if _floor_neighbors(grid, x, y) <= 2]


def _deterministic_sample(items: list, k: int, seed: int) -> list:
    """Pick k items deterministically via seeded rotation; if dedup shrinks the count, the top-up pass falls back to input order."""
    if k <= 0 or not items:
        return []
    if k >= len(items):
        return list(items)
    start = seed % len(items)
    step = max(1, len(items) // k)
    out, i = [], start
    while len(out) < k:
        out.append(items[i % len(items)])
        i += step
    # de-dup preserving order
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    # top up if dedup shrank it
    for c in items:
        if len(uniq) >= k:
            break
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq[:k]


def derive_region_tactical(
    *,
    region_id: str,
    grid: list[list[int]],
    theme_key: str,
    neighbor_ids: list[str],
    hazard_setpieces: list[str],
    creature_count: int,
) -> RegionTactical:
    """Derive all tactical data for one region. Pure + deterministic."""
    seed = _seed(region_id)
    floor = _floor_cells(grid)
    features: list[TacticalFeatureCell] = []

    # 1. Chokepoints -> difficult_terrain markers (topology).
    chokes = _chokepoints(grid)
    for c in chokes:
        features.append(TacticalFeatureCell("difficult_terrain", c, "a tight squeeze — the passage narrows"))

    # 2. Water (theme), flooding the deepest-listed floor not already a choke.
    if theme_key in WATER_THEMES:
        non_choke = [c for c in floor if c not in set(chokes)]
        k = max(1, int(len(non_choke) * _WATER_FRACTION))
        for c in _deterministic_sample(non_choke, k, seed):
            features.append(TacticalFeatureCell("water", c, "black water, depth uncertain"))

    # 3. Hazard set-pieces -> one hazard marker each, on distinct floor cells.
    if hazard_setpieces:
        spots = _deterministic_sample(floor, len(hazard_setpieces), seed ^ 0x9E3779B9)
        for piece, c in zip(hazard_setpieces, spots, strict=False):
            features.append(TacticalFeatureCell("hazard", c, f"{piece.replace('_', ' ')} — unstable"))

    # 4. Token anchors: entrance anchor first floor cell; creatures spread after.
    anchors: list[TokenAnchor] = []
    if floor:
        anchors.append(TokenAnchor(floor[0], "entrance"))
        # Single-floor-cell grids (pathological) fall back to floor[0], stacking creature on the entrance cell — harmless for v1 visual-only.
        creature_pool = floor[1:] or floor
        for c in _deterministic_sample(creature_pool, creature_count, seed ^ 0x85EBCA6B):
            anchors.append(TokenAnchor(c, "creature"))

    # 5. POIs: the hazard + interactable feature cells are points of interest.
    pois = [f.cell for f in features if f.feature_type in ("hazard", "interactable")]

    # 6. Exit thresholds: deterministically assign one floor cell per neighbour.
    exit_thresholds: dict[str, tuple[int, int]] = {}
    if floor and neighbor_ids:
        picks = _deterministic_sample(floor, len(neighbor_ids), seed ^ 0xC2B2AE35)
        for nid, c in zip(neighbor_ids, picks, strict=False):
            exit_thresholds[nid] = c

    return RegionTactical(
        region_id=region_id,
        features=features,
        anchors=anchors,
        pois=pois,
        exit_thresholds=exit_thresholds,
    )
