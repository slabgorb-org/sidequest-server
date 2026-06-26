"""Story 158-37 RED — a region's theme can violate its own depth_band.

Playtest finding (pingpong 2026-06-23, beneath_sunden MP, session
2026-06-23-beneath_sunden-mp-e88e04d6): psql dungeon_map showed exp011.r2
themed ``bone_crypt`` at ``depth_score=8.216`` while ``bone_crypt``'s
``depth_band`` is ``{min: 30.0}`` — a region wearing a theme its OWN depth
excludes by 22 points (several similar cases).

Root cause (two functions disagree on which depth the theme is gated by):

  * ``materializer._stage_design`` builds the candidate ``theme_pool`` from
    ``palette.themes_for_depth(request.frontier_edge.spawn_depth_score)`` —
    ONE depth value per expansion, the frontier edge we spawn FROM
    (``materializer.py:673`` / ``:675`` / ``:682``).
  * ``generate_expansion`` then picks each new node's theme with
    ``rng.choice(theme_pool)`` (``region_graph/generator.py:78``).
  * Only LATER, in ``_stage_attach``, does ``assign_depth_scores`` compute each
    node's FINAL ``depth_score`` independently — by ordinary-route hop distance
    from the entrance (``materializer.py:1393`` -> ``region_graph/depth.py``).

So a node can be themed against the frontier's depth (deep) and then land at a
quite different final depth (shallow, because a stitch edge gives it a short
ordinary route to the surface). The theme and the node's own depth then
disagree about which band they are in.

THE INVARIANT THESE TESTS PIN (story scope, the highest spec authority):
    After the full design -> attach -> depth pipeline, every region node's
    theme must be eligible at that node's OWN final depth_score, i.e.
    ``theme_eligible_at_depth(palette.get(node.theme), node.depth_score)``.

Doctrine note / known tension (surfaced as a Delivery Finding for Keith):
sibling story 158-19's test embeds Keith's 2026-06-24 "random-dungeon" steer —
"depth tunes ENCOUNTER difficulty ... NOT theme eligibility; every stratum
offers a broad grab-bag of themes." These tests are deliberately robust to that
steer: they do NOT mandate a narrowing depth->theme gradient and they do NOT
forbid depth-agnostic themes. ``test_wide_band_palette_is_already_coherent``
is the positive control proving a grab-bag of ``{min:0, max:None}`` themes is
UNAFFECTED by the fix. The only thing pinned is the No-Silent-Fallbacks rule:
a theme that DOES declare a bounded band must have that band honored against
the node's own depth, never silently violated. Exact real depth_band values
remain a content/pacing call owned by Keith — every fixture here is synthetic.
"""

from __future__ import annotations

from typing import Any

from sidequest.dungeon.persistence import FrontierEdge
from sidequest.dungeon.region_graph.config import JaquaysConfig
from sidequest.dungeon.region_graph.depth import assign_depth_scores
from sidequest.dungeon.region_graph.generator import attach_expansion, generate_expansion
from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.dungeon.themes import (
    DungeonTheme,
    ThemePalette,
    theme_eligible_at_depth,
)

# --------------------------------------------------------------------------- #
# Helpers (inlined per CLAUDE.md — no reaching across test modules)
# --------------------------------------------------------------------------- #

_CAMPAIGN_SEED = 7  # matches tests/dungeon/region_graph/test_generator.py


def _theme(tid: str, *, band_min: float, band_max: float | None) -> DungeonTheme:
    """A minimal valid theme carrying only the depth_band under test.

    generator_class 'organic' pins interior.algorithm 'cellular'
    (themes._CLASS_ALGORITHM); no set_pieces/quest_template needed here.
    """
    return DungeonTheme.model_validate(
        {
            "id": tid,
            "display_name": tid.replace("_", " ").title(),
            "generator_class": "organic",
            "interior": {"algorithm": "cellular", "braid_ratio": 0.0},
            "depth_band": {"min": band_min, "max": band_max},
            "narrator": {
                "register": "grave",
                "flavor": f"placeholder flavor for {tid}",
                "motifs": [],
            },
        }
    )


def _palette(*themes: DungeonTheme) -> ThemePalette:
    return ThemePalette(themes={t.id: t for t in themes})


def _explored_graph() -> RegionGraph:
    """A small explored map with FROZEN depth_scores on every node.

    Mirrors the loopful 5-node shape from test_generator._explored() so
    generate_expansion reliably finds a Jaquays-valid candidate. Depths are
    pre-set (frozen) so assign_depth_scores only scores the NEW expansion
    nodes — exactly the production contract (spec §7: scores frozen at attach,
    never recomputed). ``deep_anchor`` (e3) carries a deep frozen score: it is
    the region we expand off of, even though a discovered loop makes it
    ordinarily shallow now — the realistic frozen-depth subtlety.
    """
    g = RegionGraph(entrance_id="surface")
    g.add_node(RegionNode(id="surface", expansion_id=0, theme="shallow_theme", depth_score=0.0))
    g.add_node(RegionNode(id="e0", expansion_id=1, theme="shallow_theme", depth_score=10.0))
    g.add_node(RegionNode(id="e1", expansion_id=1, theme="shallow_theme", depth_score=20.0))
    g.add_node(RegionNode(id="e2", expansion_id=1, theme="shallow_theme", depth_score=30.0))
    g.add_node(RegionNode(id="deep_anchor", expansion_id=1, theme="deep_theme", depth_score=100.0))
    chain = ["surface", "e0", "e1", "e2", "deep_anchor"]
    for a, b in zip(chain, chain[1:], strict=False):
        g.add_edge(RegionEdge(a=a, b=b, kind="corridor"))
    g.add_edge(RegionEdge(a="surface", b="deep_anchor", kind="stairs"))  # explored loop
    return g


def _otel_span() -> Any:
    """A real (in-memory) recording span for _stage_design's set_attribute
    calls — never the no-op INVALID_SPAN."""
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test-158-37")
    return tracer.start_span("test.stage_design")


def _band_violations(graph: RegionGraph, palette: ThemePalette, new_ids: set[str]) -> list[dict]:
    """Every new node whose theme is NOT eligible at its own final depth."""
    out: list[dict] = []
    for rid in new_ids:
        node = graph.nodes[rid]
        assert node.depth_score is not None, f"{rid} has no depth_score after assign"
        theme = palette.get(node.theme)
        if not theme_eligible_at_depth(theme, node.depth_score):
            out.append(
                {
                    "region": rid,
                    "theme": node.theme,
                    "depth_score": node.depth_score,
                    "band_min": theme.depth_band.min,
                    "band_max": theme.depth_band.max,
                }
            )
    return out


# --------------------------------------------------------------------------- #
# Test 1 — HEADLINE invariant (real generate_expansion + attach + depth)
# --------------------------------------------------------------------------- #


def test_every_region_theme_is_eligible_at_its_own_final_depth_score():
    """The invariant the fix must establish: after the pipeline, EVERY region's
    theme is eligible at that region's own final depth_score.

    Forcing setup: the frontier we spawn from is deep (spawn_depth_score=100),
    so the theme_pool — gated by that single frontier depth — contains only the
    deep theme. Every new node is themed deep. But the new nodes' ordinary route
    to the surface is short, so assign_depth_scores lands them shallow. Each
    ends up wearing the deep theme at a shallow depth -> band violation.

    RED today: theme is chosen against the frontier depth, not the node's own.
    """
    palette = _palette(
        _theme("shallow_theme", band_min=0.0, band_max=60.0),
        _theme("deep_theme", band_min=50.0, band_max=None),
    )
    graph = _explored_graph()

    # Mirror materializer._stage_design:675/:682 — pool from the FRONTIER depth.
    spawn_depth = 100.0
    theme_pool = [t.id for t in palette.themes_for_depth(spawn_depth)]
    assert theme_pool == ["deep_theme"], (
        "fixture precondition: only the deep theme is eligible at the deep "
        f"frontier depth; got {theme_pool}"
    )

    expansion, _report = generate_expansion(
        graph=graph,
        campaign_seed=_CAMPAIGN_SEED,
        expansion_id=2,
        attach_region_ids=["e2", "deep_anchor"],
        theme_pool=theme_pool,
        config=JaquaysConfig(connection_burst=0),
    )
    new_ids = expansion.new_region_ids()

    # Mirror _stage_attach:1383 then :1393 — attach, then assign final depths.
    attach_expansion(graph, expansion)
    assign_depth_scores(graph, campaign_seed=_CAMPAIGN_SEED)

    violations = _band_violations(graph, palette, new_ids)
    assert violations == [], (
        "every region's theme must be eligible at its OWN final depth_score; "
        f"band violations remain: {violations}"
    )


# --------------------------------------------------------------------------- #
# Test 2 — WIRING: the real production _stage_design builds the bad pool
# --------------------------------------------------------------------------- #


def test_stage_design_themed_nodes_are_eligible_at_their_final_depth():
    """Same invariant as Test 1, but routed through the REAL production
    ``materializer._stage_design`` (which builds the theme_pool from
    ``request.frontier_edge.spawn_depth_score``) followed by the SAME
    attach + assign_depth_scores calls ``_stage_attach`` makes. This is the
    wiring proof: the defect rides the production theme-pool construction, not
    just a hand-assembled call to ``generate_expansion``.

    RED today — _stage_design themes against the deep frontier depth, the nodes
    land shallow, and nothing re-resolves the theme against the node's own
    final depth.

    Fix-placement note for Dev: this test deliberately runs only the real
    design -> attach -> assign sequence. Land the re-resolution so it executes
    within that sequence (e.g. folded into the design pick once per-node depth
    is known, or a deterministic re-resolve step invoked right after
    ``assign_depth_scores`` and also called here). The CONTRACT is the
    invariant assertion below, not where the code lives.
    """
    palette = _palette(
        _theme("shallow_theme", band_min=0.0, band_max=60.0),
        _theme("deep_theme", band_min=50.0, band_max=None),
    )
    graph = _explored_graph()

    fe = FrontierEdge(
        frontier_edge_id="fe_deep",
        from_region_id="deep_anchor",
        heading="down",
        spawn_depth_score=100.0,  # deep frontier -> deep-only theme_pool
    )
    from sidequest.dungeon.materializer import MaterializationRequest, _stage_design

    req = MaterializationRequest.build(
        campaign_seed=_CAMPAIGN_SEED,
        expansion_id=2,
        frontier_edge=fe,
        attach_region_ids=["e2", "deep_anchor"],
        heading="down",
        burst_magnitude=1,
        lookahead_breadth=1,
        frontier=[fe],
    )

    span = _otel_span()
    try:
        expansion, _report = _stage_design(req, graph=graph, palette=palette, span=span)
    finally:
        span.end()

    new_ids = expansion.new_region_ids()
    attach_expansion(graph, expansion)
    assign_depth_scores(graph, campaign_seed=_CAMPAIGN_SEED)

    violations = _band_violations(graph, palette, new_ids)
    assert violations == [], (
        "every region themed through the production _stage_design path must be "
        "eligible at its OWN final depth_score; band violations remain: "
        f"{violations}"
    )


# --------------------------------------------------------------------------- #
# Test 3 — POSITIVE CONTROL: a depth-agnostic grab-bag is already coherent
# --------------------------------------------------------------------------- #


def test_wide_band_palette_is_already_coherent():
    """Keith's random-dungeon grab-bag (2026-06-24): themes with unbounded
    bands ({min:0, max:None}) are eligible at every depth. The invariant holds
    for them with NO fix — proving (a) the headline test is not a tautology
    (it is the BOUNDED-band case that fails) and (b) the fix must not break the
    grab-bag by over-narrowing theme eligibility.

    GREEN now and after the fix.
    """
    palette = _palette(
        _theme("grab_a", band_min=0.0, band_max=None),
        _theme("grab_b", band_min=0.0, band_max=None),
        _theme("grab_c", band_min=0.0, band_max=None),
    )
    graph = _explored_graph()
    # depth_score on the explored anchors uses themes that must exist in THIS
    # palette for palette.get() during violation scan; rename anchors' themes.
    for rid, node in list(graph.nodes.items()):
        graph.nodes[rid] = RegionNode(
            id=node.id,
            expansion_id=node.expansion_id,
            theme="grab_a",
            depth_score=node.depth_score,
        )

    spawn_depth = 100.0
    theme_pool = [t.id for t in palette.themes_for_depth(spawn_depth)]
    assert sorted(theme_pool) == ["grab_a", "grab_b", "grab_c"], (
        "grab-bag precondition: every theme eligible at any depth"
    )

    expansion, _report = generate_expansion(
        graph=graph,
        campaign_seed=_CAMPAIGN_SEED,
        expansion_id=2,
        attach_region_ids=["e2", "deep_anchor"],
        theme_pool=theme_pool,
        config=JaquaysConfig(connection_burst=0),
    )
    new_ids = expansion.new_region_ids()

    attach_expansion(graph, expansion)
    assign_depth_scores(graph, campaign_seed=_CAMPAIGN_SEED)

    violations = _band_violations(graph, palette, new_ids)
    assert violations == [], (
        "depth-agnostic grab-bag themes must never violate (they are eligible "
        f"everywhere); unexpected violations: {violations}"
    )
