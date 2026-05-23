"""depth_score gradient (spec: Beneath Sünden §5, §10 step 3).

depth_score is the ONLY notion of depth: an abstract scalar attached to
each region AT ATTACH TIME and frozen into the save (never recomputed).
It is ≈ ordinary-route graph distance from the surface entrance,
deterministically jittered. "Level" survives only as a coarse,
approximate player-facing bucket — never an authoritative coordinate,
key, or container (spec decision rows 1, 2; §5).

Ordinary-route distance EXCLUDES hidden + shortcut edges: a secret
passage is not the ordinary route and a shortcut is a discovered bypass
(same rationale as invariants.py's `stitch` exclusion). Discovering a
shortcut later must NOT retroactively make a region shallower — scores
are frozen at attach.

Pure, deterministic, dependency-free. No OTEL / session wiring here —
Plan 7's materializer emits the `dungeon.materialize.attach` span from
DepthReport.as_dict() (same honest-deferral stance as Plan 2's
region_graph: see __init__.py docstring "later plans (3/4/5/7)").
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass

from sidequest.dungeon.region_graph.model import RegionGraph


@dataclass(frozen=True)
class DepthConfig:
    """Tunable depth knobs (spec §5 'All thresholds ... tunable in world config')."""

    depth_per_hop: float = 10.0
    jitter_max: float = 3.0
    # §12 decision: coarse shorthand = 3 ordinary hops per "level".
    # Deliberately NOT 1:1 (a 1-hop bucket would resurrect the rejected
    # discrete-floor concept — spec decision rows 1-2).
    bucket_size: float = 30.0

    def validate(self) -> None:
        if self.depth_per_hop <= 0.0:
            raise ValueError("depth_per_hop must be > 0")
        if self.jitter_max < 0.0:
            raise ValueError("jitter_max must be >= 0")
        if self.jitter_max >= self.depth_per_hop:
            raise ValueError(
                "jitter_max must be < depth_per_hop (else a dist-1 region "
                "can jitter below 0 and level_bucket floors it to the "
                "surface — a silent depth collapse)"
            )
        if self.bucket_size < self.depth_per_hop:
            raise ValueError(
                "bucket_size must be >= depth_per_hop (a player-facing "
                "'level' must be coarser than a single hop, else it is a "
                "floor index — the explicitly-rejected concept)"
            )


@dataclass
class DepthReport:
    """Span-ready contract — Plan 7's materializer turns this into
    `dungeon.materialize.attach` OTEL attributes (mirrors
    GenerationReport.as_dict() from invariants.py)."""

    regions_scored: int = 0
    depth_min: float = 0.0
    depth_max: float = 0.0
    depth_mean: float = 0.0

    def as_dict(self) -> dict:
        return {
            "regions_scored": self.regions_scored,
            "depth_min": self.depth_min,
            "depth_max": self.depth_max,
            "depth_mean": self.depth_mean,
        }


def ordinary_route_dist(graph: RegionGraph) -> dict[str, int]:
    """BFS hop distance from the entrance over ORDINARY edges only —
    hidden (secret) and shortcut edges are excluded (a secret passage is
    not the ordinary route; a shortcut is a discovered bypass — same
    exclusion rationale as invariants.py's `stitch`).

    Raises loudly (CLAUDE.md: No Silent Fallbacks) if any region is not
    reachable from the entrance on the ordinary-route graph — depth must
    never silently default to 0 for an unreachable region.
    """
    skip = {i for i, e in enumerate(graph.edges) if e.hidden or e.shortcut}
    dist = graph.bfs_dist(graph.entrance_id, skip_edges=skip)
    missing = sorted(set(graph.nodes) - set(dist))
    if missing:
        raise ValueError(
            f"regions {missing} not reachable on the ordinary route "
            f"from {graph.entrance_id!r} (hidden/shortcut edges excluded); "
            f"cannot assign a depth_score"
        )
    return dist


def depth_jitter(*, campaign_seed: int, region_id: str, jitter_max: float) -> float:
    """Deterministic per-region jitter in [-jitter_max, +jitter_max].

    Sub-seeds with blake2b, NOT XOR — mirrors generator._subseed and
    refuses to reproduce the `seed ^ 0x5EED` fixed-point-at-24301 class
    of bug at this layer (Beneath Sünden carry-forward gotcha).
    """
    if jitter_max == 0.0:
        return 0.0
    digest = hashlib.blake2b(
        f"{campaign_seed}|depth|{region_id}".encode(),
        digest_size=8,
    ).digest()
    # map the 64-bit digest to a float in [0.0, 1.0], then to
    # [-jitter_max, +jitter_max].  (Not strictly [0,1): 2**64-1 rounds
    # to 2**64 in IEEE 754 double, so both extremes are reachable.)
    frac = int.from_bytes(digest, "big") / float(1 << 64)
    return (frac * 2.0 - 1.0) * jitter_max


def assign_depth_scores(
    graph: RegionGraph,
    *,
    campaign_seed: int,
    config: DepthConfig | None = None,
) -> DepthReport:
    """Assign depth_score to every region that does not yet have one,
    then FREEZE it (already-scored regions are never recomputed — the
    save is the source of truth, spec §7).

    Score = ordinary-route hops from the entrance * depth_per_hop +
    deterministic bounded jitter. The entrance is the origin: exactly
    0.0, no jitter. Mutates graph.nodes in place (replacing frozen
    RegionNode instances) and returns the same graph's DepthReport —
    mirrors attach_expansion's "returns the (mutated) graph" contract
    and GenerationReport's span-ready report precedent.

    Raises loudly if a to-be-scored region is unreachable on the
    ordinary-route graph (CLAUDE.md: No Silent Fallbacks).
    """
    cfg = config or DepthConfig()
    cfg.validate()

    to_score = [rid for rid, n in graph.nodes.items() if n.depth_score is None]
    if not to_score:
        return DepthReport(regions_scored=0)

    dist = ordinary_route_dist(graph)  # raises if any region unreachable

    scored_values: list[float] = []
    for rid in to_score:
        if rid == graph.entrance_id:
            score = 0.0
        else:
            base = dist[rid] * cfg.depth_per_hop
            score = base + depth_jitter(
                campaign_seed=campaign_seed,
                region_id=rid,
                jitter_max=cfg.jitter_max,
            )
        graph.nodes[rid] = dataclasses.replace(graph.nodes[rid], depth_score=score)
        scored_values.append(score)

    return DepthReport(
        regions_scored=len(scored_values),
        depth_min=min(scored_values),
        depth_max=max(scored_values),
        depth_mean=sum(scored_values) / len(scored_values),
    )


def level_bucket(depth_score: float, config: DepthConfig | None = None) -> int:
    """Coarse player-facing 'level' bucket (spec §5, §12 decision).

    APPROXIMATION ONLY — never an authoritative coordinate, key, or
    container (spec decision rows 1, 2). 0 == at/just inside the surface
    threshold; each bucket spans `bucket_size` (default 3 ordinary hops).
    """
    cfg = config or DepthConfig()
    cfg.validate()
    if depth_score <= 0.0:
        return 0
    return int(depth_score // cfg.bucket_size)


def level_phrase(depth_score: float, config: DepthConfig | None = None) -> str:
    """Fuzzy player-facing shorthand (spec §5 example: 'you reckon you're
    four, maybe five levels down'). Deliberately approximate; this is the
    coarse mechanical label only — narrator/curation handles prose."""
    cfg = config or DepthConfig()
    cfg.validate()
    if depth_score <= 0.0:
        return "at the surface, just inside the threshold"
    b = int(depth_score // cfg.bucket_size)
    pos = depth_score % cfg.bucket_size
    if cfg.bucket_size - pos <= cfg.jitter_max:
        # near the UPPER edge: about to cross into b+1
        deeper = b + 1
        unit = "level" if deeper == 1 else "levels"
        return f"you reckon you're {b}, maybe {deeper} {unit} down"
    if b >= 1 and pos <= cfg.jitter_max:
        # near the LOWER edge: might still read as b-1 (never below 0)
        unit = "level" if b == 1 else "levels"
        return f"you reckon you're {b - 1}, maybe {b} {unit} down"
    unit = "level" if b == 1 else "levels"
    return f"about {b} {unit} down"
