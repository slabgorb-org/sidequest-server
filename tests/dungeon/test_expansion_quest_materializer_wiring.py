"""Plan 7 Task 7 — Mandatory wiring test: seed_expansion_quest in _stage_attach.

Verifies that:
  (a) After _stage_attach runs on an expansion whose theme carries a
      quest_template, exactly ONE open expansion-scoped quest thread exists
      in the DungeonStore (kind=="quest", payload["scope"]=="expansion",
      correct expansion_id).
  (b) A dungeon.quest.bound span was emitted.
  (c) When the theme has NO quest_template, no quest thread is seeded
      (no-op guard — the guard is the whole point of this task).

Mirrors the fixture setup in tests/dungeon/test_setpiece_attach_wiring.py:
real in-memory DungeonStore, real _stage_attach call, real OTEL capture.
No mocking of the dungeon layer.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import sidequest.telemetry.spans as _spans_module


# ---------------------------------------------------------------------------
# Shared fixture helpers (inlined; CLAUDE.md: do not reach across test
# modules into underscore-prefixed helpers)
# ---------------------------------------------------------------------------


def _store_with_schema() -> tuple[Any, Any]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from sidequest.dungeon.persistence import DungeonStore

    store = DungeonStore(conn)
    store.ensure_schema()
    return conn, store


def _fresh_snapshot() -> Any:
    from sidequest.game.session import GameSnapshot

    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="test_world")


def _theme_with_quest_template(theme_id: str) -> Any:
    """A real DungeonTheme (cellular/organic) carrying one SetPiece AND a
    quest_template with signature='reach_deep'.  The quest_template is what
    Task 7 guards on."""
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


def _theme_without_quest_template(theme_id: str) -> Any:
    """Same shape but quest_template=None — the no-op path."""
    from sidequest.dungeon.setpieces import SetPiece
    from sidequest.dungeon.themes import (
        Adjacency,
        DepthBand,
        DungeonTheme,
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
    return DungeonTheme(
        id=theme_id,
        display_name=theme_id.replace("_", " ").title(),
        generator_class="organic",
        interior=InteriorSpec(algorithm="cellular", braid_ratio=0.0),
        depth_band=DepthBand(min=0.0, max=None),
        narrator=NarratorFlavor(register="grave", flavor="dread whispers"),
        adjacency=Adjacency(),
        set_pieces=[sp],
        quest_template=None,
    )


def _expansion_off_seed(theme_id: str, expansion_id: int = 1) -> Any:
    """Two new regions hung off entrance with a loop — connected AND loopful."""
    from sidequest.dungeon.region_graph import Expansion, RegionEdge, RegionNode

    r0 = f"exp{expansion_id:03d}.r0"
    r1 = f"exp{expansion_id:03d}.r1"
    nodes = [
        RegionNode(id=r0, expansion_id=expansion_id, theme=theme_id),
        RegionNode(id=r1, expansion_id=expansion_id, theme=theme_id),
    ]
    edges = [
        RegionEdge(a="entrance", b=r0, kind="corridor"),
        RegionEdge(a=r0, b=r1, kind="corridor"),
        RegionEdge(a=r1, b="entrance", kind="stairs"),
    ]
    return Expansion(expansion_id=expansion_id, new_nodes=nodes, new_edges=edges)


def _seed_graph(theme_id: str) -> Any:
    """Graph with entrance only, re-themed for palette resolution."""
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
        frontier_edge_id="fe_quest_wire",
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
    campaign_seed: int = 42,
    expansion_id: int = 1,
    store: Any,
) -> Any:
    """Run the REAL _stage_attach with the given theme, returning AttachResult."""
    import sidequest.dungeon.materializer as _mat
    from sidequest.dungeon.themes import ThemePalette
    from sidequest.telemetry.spans.dungeon_materialize import dungeon_materialize_attach_span

    palette = ThemePalette(themes={theme_id: theme})
    graph = _seed_graph(theme_id)
    expansion = _expansion_off_seed(theme_id, expansion_id=expansion_id)
    curation = _curation_for(expansion)
    snapshot = _fresh_snapshot()
    pack = SimpleNamespace(tropes=[])  # no trope components on set-piece
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
# (a) + (b): theme WITH quest_template seeds exactly one expansion-quest
#            thread and emits dungeon.quest.bound.
# ---------------------------------------------------------------------------

EXPANSION_ID = 5
CAMPAIGN_SEED = 99


def test_attach_seeds_one_expansion_quest_thread() -> None:
    """_stage_attach with a theme that carries a quest_template seeds exactly
    one open expansion-scoped quest thread in the DungeonStore.

    (a) Exactly one thread with kind=="quest" and payload["scope"]=="expansion"
        exists after attach.
    (b) The thread's payload["expansion_id"] matches the request's expansion_id.
    (c) A dungeon.quest.bound span was emitted.
    """
    from sidequest.telemetry.spans.dungeon_quest import SPAN_QUEST_BOUND

    theme_id = "quest_wire_crypt"
    conn, store = _store_with_schema()

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        _run_stage_attach(
            theme_id=theme_id,
            theme=_theme_with_quest_template(theme_id),
            campaign_seed=CAMPAIGN_SEED,
            expansion_id=EXPANSION_ID,
            store=store,
        )
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]
    conn.commit()

    # (a) Exactly one open expansion-scoped quest thread.
    quest_threads = [
        t
        for t in store.open_threads()
        if t.kind == "quest" and t.payload.get("scope") == "expansion"
    ]
    assert len(quest_threads) == 1, (
        f"expected 1 open expansion-quest thread, got {len(quest_threads)}; "
        f"all open: {[(t.thread_id, t.kind, t.payload) for t in store.open_threads()]}"
    )

    # (b) Correct expansion_id in payload.
    assert quest_threads[0].payload["expansion_id"] == EXPANSION_ID, (
        f"quest thread expansion_id={quest_threads[0].payload['expansion_id']!r}, "
        f"expected {EXPANSION_ID!r}"
    )

    # (c) dungeon.quest.bound span emitted.
    finished = exporter.get_finished_spans()
    bound_spans = [s for s in finished if s.name == SPAN_QUEST_BOUND]
    assert bound_spans, (
        "dungeon.quest.bound span NOT emitted — seed_expansion_quest did not "
        "fire its OTEL span; the GM panel cannot verify the quest engine engaged"
    )


# ---------------------------------------------------------------------------
# (c) theme WITHOUT quest_template is a clean no-op — no quest thread seeded.
# ---------------------------------------------------------------------------


def test_attach_no_quest_template_is_no_op() -> None:
    """_stage_attach with a theme that has quest_template=None seeds NO
    expansion-scoped quest thread — the guard is a clean no-op."""
    theme_id = "noquesttemplate_crypt"
    conn, store = _store_with_schema()

    _run_stage_attach(
        theme_id=theme_id,
        theme=_theme_without_quest_template(theme_id),
        campaign_seed=CAMPAIGN_SEED,
        expansion_id=EXPANSION_ID,
        store=store,
    )
    conn.commit()

    quest_threads = [
        t
        for t in store.open_threads()
        if t.kind == "quest" and t.payload.get("scope") == "expansion"
    ]
    assert len(quest_threads) == 0, (
        f"expected 0 expansion-quest threads when theme has no quest_template, "
        f"got {len(quest_threads)}: {[(t.thread_id, t.payload) for t in quest_threads]}"
    )
