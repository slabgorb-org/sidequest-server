"""Plan 3 §11 property sweep + region_graph integration (wiring) test.

Production session-path wiring + dungeon.materialize.attach OTEL spans
are Plan 7's materializer scope (honest deferral, same as Plan 2's
region_graph). This proves depth.py is wired to the REAL region_graph
generator/attach path, not unit-isolated.
"""

import pytest

from sidequest.dungeon.region_graph import (
    DepthConfig,
    RegionGraph,
    RegionNode,
    assign_depth_scores,
    attach_expansion,
    generate_expansion,
    level_bucket,
)
from sidequest.dungeon.region_graph.depth import ordinary_route_dist

THEMES = ["crypt", "cavern", "catacomb", "vault", "flooded"]


def _seed_graph() -> RegionGraph:
    g = RegionGraph(entrance_id="surface")
    g.add_node(RegionNode(id="surface", expansion_id=0, theme="threshold"))
    return g


def _grow(campaign_seed: int, expansions: int):
    """Build a multi-expansion contiguous map via the REAL Plan 2
    generator + attach, assigning depth after every attach (this is the
    integration/wiring contract Plan 7's materializer will drive)."""
    g = _seed_graph()
    cfg = DepthConfig()
    for exp_id in range(1, expansions + 1):
        if exp_id == 1:
            attach_ids = ["surface"]
        else:
            recent = [n.id for n in list(g.nodes.values())[-4:]]
            attach_ids = recent[:2] if len(recent) >= 2 else ["surface"]
        exp, _report = generate_expansion(
            graph=g,
            campaign_seed=campaign_seed,
            expansion_id=exp_id,
            attach_region_ids=attach_ids,
            theme_pool=THEMES,
        )
        attach_expansion(g, exp)
        assign_depth_scores(g, campaign_seed=campaign_seed, config=cfg)
    return g, cfg


@pytest.mark.parametrize("campaign_seed", [1, 7, 42, 100, 24301, 999999])
def test_depth_score_monotonic_ish_within_jitter(campaign_seed):
    g, cfg = _grow(campaign_seed, expansions=5)
    dist = ordinary_route_dist(g)
    for rid, node in g.nodes.items():
        assert node.depth_score is not None
        if rid == g.entrance_id:
            assert node.depth_score == 0.0
            continue
        base = dist[rid] * cfg.depth_per_hop
        assert abs(node.depth_score - base) <= cfg.jitter_max


@pytest.mark.parametrize("campaign_seed", [1, 7, 42, 100, 24301, 999999])
def test_bucket_non_decreasing_along_ordinary_paths(campaign_seed):
    g, cfg = _grow(campaign_seed, expansions=5)
    dist = ordinary_route_dist(g)
    buckets_by_dist: dict[int, list[int]] = {}
    for rid, node in g.nodes.items():
        assert node.depth_score is not None
        buckets_by_dist.setdefault(dist[rid], []).append(level_bucket(node.depth_score, cfg))
    prev_max = -1
    for d in sorted(buckets_by_dist):
        bs = buckets_by_dist[d]
        assert min(bs) >= prev_max  # every node here is >= deepest shallower bucket
        prev_max = max(prev_max, max(bs))


def test_freeze_holds_across_real_expansions():
    """Scores assigned at one attach are byte-identical after later
    expansions, AND only the new regions are (re)scored (spec §7: save
    is source of truth, never recomputed)."""
    campaign_seed = 42
    g, cfg = _grow(campaign_seed, expansions=3)
    snapshot = {rid: n.depth_score for rid, n in g.nodes.items()}
    pre_count = len(g.nodes)
    exp, _ = generate_expansion(
        graph=g,
        campaign_seed=campaign_seed,
        expansion_id=99,
        attach_region_ids=[n.id for n in list(g.nodes.values())[-2:]],
        theme_pool=THEMES,
    )
    attach_expansion(g, exp)
    report = assign_depth_scores(g, campaign_seed=campaign_seed, config=cfg)
    new_ids = set(g.nodes) - set(snapshot)
    # decisive freeze proof: ONLY the newly-attached regions were scored;
    # a broken freeze would rescore all len(g.nodes) regions.
    assert report.regions_scored == len(new_ids)
    assert 0 < report.regions_scored < len(g.nodes)
    assert len(new_ids) == len(g.nodes) - pre_count
    for rid, old in snapshot.items():
        assert g.nodes[rid].depth_score == old  # byte-identical, untouched


def test_wiring_depth_consumes_real_region_graph_public_surface():
    """Wiring assertion: the depth API is reachable from the region_graph
    package's public surface and operates on real generator output."""
    g, _ = _grow(7, expansions=5)  # 5 matches the sweeps; >=4 needed to reach bucket 1
    scores: list[float] = [n.depth_score for n in g.nodes.values() if n.depth_score is not None]
    assert len(scores) == len(g.nodes)
    assert any(level_bucket(s) >= 1 for s in scores)
