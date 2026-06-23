"""Task 12 — Mandatory end-to-end wiring + determinism test.

Exercises the full per-expansion quest lifecycle through REAL functions:

  1. Seed   — attach a synthetic expansion (theme with a ``reach_deep``
              ``quest_template``) via the REAL ``_stage_attach``; assert a
              ``dungeon.quest.bound`` span fired and one expansion-scoped
              quest thread was opened in the ledger.

  2. Project — register the REAL ``make_expansion_quest_observer`` via
               ``register_frontier_observer``; call the REAL
               ``notify_region_transition`` into the expansion;  assert a
               ``dungeon:expN`` QuestEntry appears in ``snapshot.quest_log``
               with status "active".

  3. Resolve — transition into the anchor (deepest) region; assert the
               QuestEntry flips to "completed", the ledger thread is
               resolved (``open_threads()`` empty), and a
               ``dungeon.quest.resolved`` span fired.

  4. Determinism — materialise/seed the same expansion twice from ONE
                   ``campaign_seed`` into two independent in-memory stores;
                   assert identical title/objective/signature binding.

No mocks.  Real OTEL in-memory exporter (same pattern as
tests/dungeon/test_setpiece_attach_wiring.py).

The test uses ``reach_deep`` signature; big_bad is deferred per spec.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import sidequest.telemetry.spans as _spans_module

# ---------------------------------------------------------------------------
# Shared helpers (inlined; CLAUDE.md prohibits reaching across test modules
# into underscore-prefixed helpers)
# ---------------------------------------------------------------------------


def _store_with_schema() -> tuple[Any, Any]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from sidequest.dungeon.persistence import DungeonStore

    s = DungeonStore(conn)
    s.ensure_schema()
    return conn, s


def _fresh_snapshot() -> Any:
    from sidequest.game.session import GameSnapshot

    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")


def _theme_with_reach_deep_template(theme_id: str) -> Any:
    """A real DungeonTheme carrying a reach_deep ExpansionQuestTemplate."""
    from sidequest.dungeon.setpieces import SetPiece
    from sidequest.dungeon.themes import (
        Adjacency,
        DepthBand,
        DungeonTheme,
        ExpansionQuestTemplate,
        InteriorSpec,
        NarratorFlavor,
    )

    sp = SetPiece.model_validate(
        {
            "id": f"{theme_id}_altar",
            "name": "The Altar",
            "telegraph": "A cracked altar.",
            "outcome": "The dark answers.",
            "slots": [{"name": "layout", "options": [{"value": "pit", "weight": 1.0}]}],
            "trope_components": [],
            "quest_components": [],
        }
    )
    qt = ExpansionQuestTemplate(
        signature="reach_deep",
        title="Descend the {theme}",
        objective="Reach the deepest chamber of the {theme}.",
    )
    return DungeonTheme(
        id=theme_id,
        display_name=theme_id.replace("_", " ").title(),
        generator_class="organic",
        interior=InteriorSpec(algorithm="cellular", braid_ratio=0.0),
        depth_band=DepthBand(min=0.0, max=None),
        narrator=NarratorFlavor(register="grave", flavor="dread whispers"),
        adjacency=Adjacency(),
        set_pieces=[sp],
        quest_template=qt,
    )


def _expansion_off_seed(theme_id: str, expansion_id: int = 1) -> Any:
    """Two new regions hung off entrance with a loop."""
    from sidequest.dungeon.region_graph import Expansion, RegionEdge, RegionNode

    r0 = f"exp{expansion_id:03d}.r0"
    r1 = f"exp{expansion_id:03d}.r1"
    nodes = [
        RegionNode(id=r0, expansion_id=expansion_id, theme=theme_id),
        RegionNode(id=r1, expansion_id=expansion_id, theme=theme_id, depth_score=50.0),
    ]
    edges = [
        RegionEdge(a="entrance", b=r0, kind="corridor"),
        RegionEdge(a=r0, b=r1, kind="corridor"),
        RegionEdge(a=r1, b="entrance", kind="stairs"),
    ]
    return Expansion(expansion_id=expansion_id, new_nodes=nodes, new_edges=edges)


def _seed_graph(theme_id: str) -> Any:
    from sidequest.dungeon.region_graph import RegionGraph, RegionNode

    graph = RegionGraph(entrance_id="entrance")
    graph.add_node(RegionNode(id="entrance", expansion_id=0, theme=theme_id))
    return graph


def _curation_for(expansion: Any) -> Any:
    from sidequest.dungeon.materializer import RegionCuration
    from sidequest.game.cookbook.models import RegionContentManifest

    rids = [n.id for n in expansion.new_nodes]
    manifests = {
        rid: RegionContentManifest(
            race="dwarf",
            cr_band="mid",
            size_budget={},
            wandering_table=[],
            loot_table=[],
            special_rooms=[],
            big_bad=None,
        )
        for rid in rids
    }
    return RegionCuration(
        region_manifests=manifests,
        region_creatures={rid: [] for rid in rids},
        region_big_bad={rid: None for rid in rids},
        region_look={rid: "delvehold" for rid in rids},
    )


def _build_request(campaign_seed: int = 42, expansion_id: int = 1) -> Any:
    from sidequest.dungeon.materializer import MaterializationRequest
    from sidequest.dungeon.persistence import FrontierEdge

    fe = FrontierEdge(
        frontier_edge_id="fe_e2e",
        from_region_id="entrance",
        heading="north",
        spawn_depth_score=15.0,
    )
    return MaterializationRequest.build(
        campaign_seed=campaign_seed,
        expansion_id=expansion_id,
        frontier_edge=fe,
        frontier=[fe],
        attach_region_ids=["entrance"],
        heading="north",
        burst_magnitude=3,
        lookahead_breadth=2,
    )


def _otel_in_memory() -> tuple[Any, Any, Any]:
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: PLC0415
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    real_tracer = provider.get_tracer("test")
    return exporter, provider, real_tracer


def _run_stage_attach(
    *,
    theme_id: str,
    theme: Any,
    campaign_seed: int,
    expansion_id: int,
    store: Any,
) -> Any:
    """Run the REAL _stage_attach returning AttachResult."""
    import sidequest.dungeon.materializer as _mat
    from sidequest.dungeon.themes import ThemePalette
    from sidequest.telemetry.spans.dungeon_materialize import dungeon_materialize_attach_span

    palette = ThemePalette(themes={theme_id: theme})
    graph = _seed_graph(theme_id)
    expansion = _expansion_off_seed(theme_id, expansion_id=expansion_id)
    curation = _curation_for(expansion)
    snapshot = _fresh_snapshot()
    pack = SimpleNamespace(tropes=[])
    request = _build_request(campaign_seed=campaign_seed, expansion_id=expansion_id)

    with dungeon_materialize_attach_span(expansion_id=request.expansion_id) as span:
        result = _mat._stage_attach(
            request,
            graph=graph,
            expansion=expansion,
            palette=palette,
            curation=curation,
            snapshot=snapshot,
            pack_tropes=pack,
            tx=store,
            span=span,
        )
    return result


# ---------------------------------------------------------------------------
# Test 1 + 2 + 3: seed → project → resolve, with OTEL span assertions
# ---------------------------------------------------------------------------

THEME_ID = "e2e_bone_crypt"
EXPANSION_ID = 3
CAMPAIGN_SEED = 7


def test_seed_project_resolve_and_spans() -> None:
    """Full end-to-end: attach (seed) → notify_region_transition (project) →
    notify_region_transition into anchor (resolve).

    Asserts:
      (seed)    dungeon.quest.bound span emitted; one open expansion-quest
                thread in ledger.
      (project) 'dungeon:exp3' appears in snapshot.quest_log with
                status='active' after entering the first expansion region.
      (resolve) status flips to 'completed', ledger thread resolved, and
                dungeon.quest.resolved span emitted after entering the anchor
                (deepest) region.
    """
    from sidequest.dungeon.expansion_quest import make_expansion_quest_observer
    from sidequest.dungeon.frontier_hook import (
        notify_region_transition,
        register_frontier_observer,
        unregister_frontier_observer,
    )
    from sidequest.telemetry.spans.dungeon_quest import SPAN_QUEST_BOUND, SPAN_QUEST_RESOLVED

    conn, store = _store_with_schema()
    theme = _theme_with_reach_deep_template(THEME_ID)

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        # ------------------------------------------------------------------ #
        # Step 1: Seed — _stage_attach with reach_deep quest_template
        # ------------------------------------------------------------------ #
        _run_stage_attach(
            theme_id=THEME_ID,
            theme=theme,
            campaign_seed=CAMPAIGN_SEED,
            expansion_id=EXPANSION_ID,
            store=store,
        )
        conn.commit()

        # quest.seed assertion: exactly one open expansion-quest thread
        quest_threads = [
            t
            for t in store.open_threads()
            if t.kind == "quest" and t.payload.get("scope") == "expansion"
        ]
        assert len(quest_threads) == 1, (
            f"expected 1 open expansion-quest thread after _stage_attach, "
            f"got {len(quest_threads)}: "
            f"{[(t.thread_id, t.payload) for t in store.open_threads()]}"
        )
        assert quest_threads[0].payload["expansion_id"] == EXPANSION_ID

        # dungeon.quest.bound span emitted
        finished_after_attach = exporter.get_finished_spans()
        bound_spans = [s for s in finished_after_attach if s.name == SPAN_QUEST_BOUND]
        assert bound_spans, (
            "dungeon.quest.bound span NOT emitted after _stage_attach — "
            "seed_expansion_quest did not fire its OTEL span; the GM panel "
            "cannot verify the quest engine engaged"
        )

        # ------------------------------------------------------------------ #
        # Step 2: Project — register observer, transition into first region
        # ------------------------------------------------------------------ #
        snap = _fresh_snapshot()
        observer = make_expansion_quest_observer(store)
        register_frontier_observer(observer)
        try:
            r0 = f"exp{EXPANSION_ID:03d}.r0"
            r1 = f"exp{EXPANSION_ID:03d}.r1"

            notify_region_transition(
                snap, pc_name="Rux", from_region=None, to_region=r0
            )

            qid = f"dungeon:exp{EXPANSION_ID}"
            assert qid in snap.quest_log, (
                f"{qid!r} not in quest_log after entering {r0!r}; "
                f"quest_log keys: {list(snap.quest_log.keys())}"
            )
            assert snap.quest_log[qid].status == "active", (
                f"expected status='active' after first entry, "
                f"got {snap.quest_log[qid].status!r}"
            )

            # ------------------------------------------------------------------ #
            # Step 3: Resolve — transition into the anchor (deepest) region
            # ------------------------------------------------------------------ #
            notify_region_transition(
                snap, pc_name="Rux", from_region=r0, to_region=r1
            )
            conn.commit()

            assert snap.quest_log[qid].status == "completed", (
                f"expected status='completed' after reaching anchor {r1!r}, "
                f"got {snap.quest_log[qid].status!r}"
            )

            # Ledger thread resolved
            remaining_open = store.open_threads()
            expansion_quest_open = [
                t
                for t in remaining_open
                if t.kind == "quest" and t.payload.get("scope") == "expansion"
            ]
            assert expansion_quest_open == [], (
                f"expansion-quest ledger thread still open after resolve; "
                f"open threads: {[(t.thread_id, t.status) for t in remaining_open]}"
            )

            # dungeon.quest.resolved span emitted
            finished_after_resolve = exporter.get_finished_spans()
            resolved_spans = [
                s for s in finished_after_resolve if s.name == SPAN_QUEST_RESOLVED
            ]
            assert resolved_spans, (
                "dungeon.quest.resolved span NOT emitted after reaching anchor "
                f"{r1!r} — resolve_expansion_quests did not fire its OTEL span; "
                "the GM panel cannot verify the quest resolved"
            )

            # Spot-check span attributes
            rs = resolved_spans[0]
            assert rs.attributes.get("expansion_id") == EXPANSION_ID, (
                f"resolved span expansion_id={rs.attributes.get('expansion_id')!r}, "
                f"expected {EXPANSION_ID}"
            )
            assert rs.attributes.get("signature_kind") == "reach_deep", (
                f"resolved span signature_kind={rs.attributes.get('signature_kind')!r}"
            )
            assert rs.attributes.get("resolving_event") == "reach_deep", (
                f"resolved span resolving_event={rs.attributes.get('resolving_event')!r}"
            )
        finally:
            unregister_frontier_observer(observer)
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Test 4: Determinism — same campaign_seed → same quest binding
# ---------------------------------------------------------------------------


def test_same_seed_same_quest() -> None:
    """Materialise/seed the same expansion twice from one campaign_seed
    (two independent in-memory stores) → assert identical title, objective,
    and signature_kind binding.  Mirrors Amendment C / test_seed_is_deterministic_thread_id.
    """
    from sidequest.dungeon.expansion_quest import seed_expansion_quest, select_signature
    from sidequest.dungeon.region_graph import Expansion, RegionNode
    from sidequest.dungeon.themes import ExpansionQuestTemplate
    from sidequest.game.cookbook.models import RegionContentManifest

    SEED = 42
    EXP_ID = 7
    THEME = "determinism_crypt"

    template = ExpansionQuestTemplate(
        signature="reach_deep",
        title="Descend the {theme}",
        objective="Reach the deepest chamber of the {theme}.",
    )

    def _make_expansion() -> Expansion:
        return Expansion(
            expansion_id=EXP_ID,
            new_nodes=[
                RegionNode(id=f"exp{EXP_ID:03d}.r0", expansion_id=EXP_ID, theme=THEME),
                RegionNode(
                    id=f"exp{EXP_ID:03d}.r1",
                    expansion_id=EXP_ID,
                    theme=THEME,
                    depth_score=80.0,
                ),
            ],
            new_edges=[],
        )

    def _make_manifests(expansion: Expansion) -> dict[str, RegionContentManifest]:
        return {
            n.id: RegionContentManifest(
                race="elf",
                cr_band="low",
                size_budget={},
                wandering_table=[],
                loot_table=[],
                special_rooms=[],
                big_bad=None,
            )
            for n in expansion.new_nodes
        }

    # Two independent in-memory stores
    _, store_a = _store_with_schema()
    _, store_b = _store_with_schema()

    exp_a = _make_expansion()
    exp_b = _make_expansion()
    manifests_a = _make_manifests(exp_a)
    manifests_b = _make_manifests(exp_b)

    tid_a = seed_expansion_quest(
        campaign_seed=SEED,
        expansion=exp_a,
        manifests_by_region=manifests_a,
        template=template,
        store=store_a,
        started_at_depth_score=30.0,
    )
    tid_b = seed_expansion_quest(
        campaign_seed=SEED,
        expansion=exp_b,
        manifests_by_region=manifests_b,
        template=template,
        store=store_b,
        started_at_depth_score=30.0,
    )

    # Thread IDs must be identical (deterministic hash).
    assert tid_a == tid_b, (
        f"thread ids differ across two seeds with same campaign_seed={SEED}: "
        f"{tid_a!r} vs {tid_b!r}"
    )

    # Thread payloads must carry identical quest text and signature binding.
    threads_a = store_a.open_threads()
    threads_b = store_b.open_threads()
    assert len(threads_a) == 1 and len(threads_b) == 1

    pa = threads_a[0].payload
    pb = threads_b[0].payload

    assert pa["title"] == pb["title"], (
        f"titles differ: {pa['title']!r} vs {pb['title']!r}"
    )
    assert pa["objective"] == pb["objective"], (
        f"objectives differ: {pa['objective']!r} vs {pb['objective']!r}"
    )
    assert pa["signature_kind"] == pb["signature_kind"], (
        f"signature_kinds differ: {pa['signature_kind']!r} vs {pb['signature_kind']!r}"
    )
    assert pa["anchor_region"] == pb["anchor_region"], (
        f"anchor_regions differ: {pa['anchor_region']!r} vs {pb['anchor_region']!r}"
    )

    # Confirm the title/objective are deterministically resolved (not empty).
    assert pa["title"] != "", "title must not be empty"
    assert pa["objective"] != "", "objective must not be empty"
    assert pa["signature_kind"] == "reach_deep", (
        f"expected reach_deep signature, got {pa['signature_kind']!r}"
    )

    # Additionally verify select_signature alone is deterministic (pure).
    b_a = select_signature(
        expansion=exp_a, manifests_by_region=manifests_a, template=template
    )
    b_b = select_signature(
        expansion=exp_b, manifests_by_region=manifests_b, template=template
    )
    assert b_a.title == b_b.title
    assert b_a.objective == b_b.objective
    assert b_a.kind == b_b.kind
    assert b_a.anchor_region == b_b.anchor_region
