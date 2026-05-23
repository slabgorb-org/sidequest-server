"""Unit tests for sidequest.dungeon.region_graph.depth."""

import dataclasses

import pytest

from sidequest.dungeon.region_graph.depth import (
    DepthConfig,
    assign_depth_scores,
    depth_jitter,
    level_bucket,
    level_phrase,
    ordinary_route_dist,
)
from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode


def test_depth_config_defaults():
    c = DepthConfig()
    assert c.depth_per_hop == 10.0
    assert c.jitter_max == 3.0
    assert c.bucket_size == 30.0  # §12 decision: 3 ordinary hops per "level"
    c.validate()  # defaults are self-consistent


@pytest.mark.parametrize(
    "kwargs, msg",
    [
        ({"depth_per_hop": 0.0}, "depth_per_hop must be > 0"),
        ({"depth_per_hop": -1.0}, "depth_per_hop must be > 0"),
        ({"jitter_max": -0.1}, "jitter_max must be >= 0"),
        ({"bucket_size": 9.9}, "bucket_size must be >= depth_per_hop"),
        ({"jitter_max": 10.0, "depth_per_hop": 10.0}, "jitter_max must be < depth_per_hop"),
    ],
    ids=["hop-zero", "hop-negative", "jitter-negative", "bucket-below-hop", "jitter-ge-hop"],
)
def test_depth_config_validate_rejects(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        DepthConfig(**kwargs).validate()


def test_depth_config_bucket_equal_to_hop_is_valid():
    DepthConfig(depth_per_hop=10.0, bucket_size=10.0).validate()  # equality is allowed


def test_depth_config_zero_jitter_is_valid():
    DepthConfig(jitter_max=0.0).validate()  # jitter is optional (spec §5)


def test_region_node_depth_score_defaults_none():
    n = RegionNode(id="r0", expansion_id=0, theme="crypt")
    assert n.depth_score is None  # unassigned until attach (real default, not a stub)


def test_region_node_depth_score_set_via_replace():
    n = RegionNode(id="r0", expansion_id=0, theme="crypt")
    scored = dataclasses.replace(n, depth_score=42.0)
    assert scored.depth_score == 42.0
    assert scored.id == "r0" and scored.theme == "crypt"
    assert n.depth_score is None  # original frozen instance untouched


def _chain_graph() -> RegionGraph:
    # entrance -corridor- a -corridor- b ; plus a SHORTCUT entrance->b
    g = RegionGraph(entrance_id="e")
    for rid in ("e", "a", "b"):
        g.add_node(RegionNode(id=rid, expansion_id=0, theme="t"))
    g.add_edge(RegionEdge(a="e", b="a", kind="corridor"))
    g.add_edge(RegionEdge(a="a", b="b", kind="corridor"))
    g.add_edge(RegionEdge(a="e", b="b", kind="shaft", shortcut=True))
    return g


def test_ordinary_route_ignores_shortcut():
    g = _chain_graph()
    d = ordinary_route_dist(g)
    # shortcut e->b is excluded: b is 2 hops via a, not 1 via the shortcut
    assert d == {"e": 0, "a": 1, "b": 2}


def test_ordinary_route_ignores_hidden():
    g = RegionGraph(entrance_id="e")
    for rid in ("e", "a", "b"):
        g.add_node(RegionNode(id=rid, expansion_id=0, theme="t"))
    g.add_edge(RegionEdge(a="e", b="a", kind="corridor"))
    g.add_edge(RegionEdge(a="a", b="b", kind="corridor"))
    g.add_edge(RegionEdge(a="e", b="b", kind="secret", hidden=True))
    assert ordinary_route_dist(g) == {"e": 0, "a": 1, "b": 2}


def test_ordinary_route_raises_when_region_unreachable_on_ordinary_graph():
    # b reachable ONLY via a hidden edge -> No Silent Fallbacks: fail loud
    g = RegionGraph(entrance_id="e")
    for rid in ("e", "b"):
        g.add_node(RegionNode(id=rid, expansion_id=0, theme="t"))
    g.add_edge(RegionEdge(a="e", b="b", kind="secret", hidden=True))
    with pytest.raises(ValueError, match="not reachable on the ordinary route"):
        ordinary_route_dist(g)


def test_ordinary_route_single_node():
    g = RegionGraph(entrance_id="e")
    g.add_node(RegionNode(id="e", expansion_id=0, theme="t"))
    assert ordinary_route_dist(g) == {"e": 0}  # seed graph: entrance only, no raise


def test_depth_jitter_deterministic():
    a = depth_jitter(campaign_seed=12345, region_id="exp001.r3", jitter_max=3.0)
    b = depth_jitter(campaign_seed=12345, region_id="exp001.r3", jitter_max=3.0)
    assert a == b


def test_depth_jitter_within_bounds():
    for rid in (f"exp{e:03d}.r{r}" for e in range(20) for r in range(6)):
        j = depth_jitter(campaign_seed=999, region_id=rid, jitter_max=3.0)
        assert -3.0 <= j <= 3.0


def test_depth_jitter_varies_by_region():
    seed = 7
    vals = {
        depth_jitter(campaign_seed=seed, region_id=f"exp000.r{i}", jitter_max=3.0)
        for i in range(40)
    }
    assert len(vals) > 1  # not a constant


def test_depth_jitter_zero_max_is_exactly_zero():
    assert depth_jitter(campaign_seed=1, region_id="exp000.r0", jitter_max=0.0) == 0.0


def test_depth_jitter_seed_24301_is_not_degenerate():
    # The seed ^ 0x5EED fixed point (24301) must NOT collapse jitter to a
    # constant here — we use blake2b, never XOR (carry-forward gotcha).
    vals = {
        depth_jitter(campaign_seed=24301, region_id=f"exp000.r{i}", jitter_max=3.0)
        for i in range(40)
    }
    assert len(vals) > 1


def _scored_chain():
    # e -corridor- a -corridor- b ; e -shortcut- b
    g = _chain_graph()
    cfg = DepthConfig(depth_per_hop=10.0, jitter_max=3.0)
    report = assign_depth_scores(g, campaign_seed=42, config=cfg)
    return g, cfg, report


def test_assign_scores_all_regions_and_entrance_is_zero():
    g, cfg, _ = _scored_chain()
    assert g.nodes["e"].depth_score == 0.0  # entrance is the origin, exactly 0
    for rid in ("a", "b"):
        assert g.nodes[rid].depth_score is not None


def test_assign_score_is_base_plus_bounded_jitter():
    g, cfg, _ = _scored_chain()
    score_a = g.nodes["a"].depth_score
    score_b = g.nodes["b"].depth_score
    assert score_a is not None and score_b is not None
    # b is 2 ordinary hops deep (shortcut excluded) -> base 20.0 +/- 3.0
    assert abs(score_b - 20.0) <= cfg.jitter_max
    assert abs(score_a - 10.0) <= cfg.jitter_max


def test_assign_is_frozen_second_call_is_noop_on_scored_regions():
    g, cfg, _ = _scored_chain()
    snapshot = {rid: n.depth_score for rid, n in g.nodes.items()}
    # add a new unscored region off 'b', re-assign: old scores MUST NOT move
    g.add_node(RegionNode(id="c", expansion_id=1, theme="t"))
    g.add_edge(RegionEdge(a="b", b="c", kind="corridor"))
    g.add_edge(RegionEdge(a="a", b="c", kind="corridor"))  # 2nd ordinary entry
    rep2 = assign_depth_scores(g, campaign_seed=42, config=cfg)
    for rid, old in snapshot.items():
        assert g.nodes[rid].depth_score == old  # frozen — save is source of truth
    assert g.nodes["c"].depth_score is not None
    assert rep2.regions_scored == 1  # only the new one


def test_assign_raises_when_region_unreachable_on_ordinary_graph():
    g = RegionGraph(entrance_id="e")
    for rid in ("e", "b"):
        g.add_node(RegionNode(id=rid, expansion_id=0, theme="t"))
    g.add_edge(RegionEdge(a="e", b="b", kind="secret", hidden=True))
    with pytest.raises(ValueError, match="not reachable on the ordinary route"):
        assign_depth_scores(g, campaign_seed=1, config=DepthConfig())


def test_depth_report_as_dict_is_stable_span_contract():
    g, cfg, report = _scored_chain()
    d = report.as_dict()
    # exact key-set is the OTEL span contract Plan 7 consumes — pin it
    assert set(d) == {
        "regions_scored",
        "depth_min",
        "depth_max",
        "depth_mean",
    }
    assert d["regions_scored"] == 3  # e, a, b
    assert d["depth_min"] == 0.0  # entrance
    assert d["depth_max"] == report.depth_max


def test_depth_report_empty_when_nothing_to_score():
    g, cfg, _ = _scored_chain()
    rep = assign_depth_scores(g, campaign_seed=42, config=cfg)  # all scored
    assert rep.regions_scored == 0
    assert rep.as_dict()["regions_scored"] == 0
    assert rep.as_dict()["depth_min"] == 0.0
    assert rep.as_dict()["depth_max"] == 0.0
    assert rep.as_dict()["depth_mean"] == 0.0


def test_level_bucket_zero_at_and_below_entrance():
    cfg = DepthConfig()  # bucket_size 30.0
    assert level_bucket(0.0, cfg) == 0
    assert level_bucket(29.9, cfg) == 0
    assert level_bucket(30.0, cfg) == 1
    assert level_bucket(95.0, cfg) == 3


def test_level_bucket_is_monotonic_non_decreasing():
    cfg = DepthConfig()
    last = -1
    for s in range(0, 400, 5):
        b = level_bucket(float(s), cfg)
        assert b >= last
        last = b


def test_level_bucket_stable_for_same_score():
    cfg = DepthConfig()
    assert level_bucket(57.3, cfg) == level_bucket(57.3, cfg)


def test_level_phrase_surface_and_depth_and_boundary_fuzz():
    cfg = DepthConfig()  # bucket_size 30, jitter_max 3
    assert "surface" in level_phrase(0.0, cfg).lower()
    # mid-bucket -> confident "about N"
    mid = level_phrase(45.0, cfg).lower()
    assert "about" in mid and "1" in mid  # 45/30 -> bucket 1
    # near a bucket boundary (within jitter_max of a multiple of 30) ->
    # fuzzy "N, maybe N+1" (spec §5: "four, maybe five levels down")
    fuzzy = level_phrase(89.0, cfg).lower()  # 89 ~ boundary 90 (bucket 2|3)
    assert "maybe" in fuzzy


def test_level_phrase_lower_edge_points_shallower():
    cfg = DepthConfig()  # bucket_size 30, jitter_max 3
    # depth 30.1: bucket 1, but only 0.1 into it -> honest fuzz is "0, maybe 1"
    lower = level_phrase(30.1, cfg).lower()
    assert "0, maybe 1" in lower
    assert "1 level down" in lower and "1 levels down" not in lower  # singular grammar
    # bucket-0 lower edge must NOT degenerate to "0, maybe 0"
    assert "maybe 0" not in level_phrase(2.0, cfg).lower()
