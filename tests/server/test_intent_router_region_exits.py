"""current_region_exits projection (Story 105-2 Piece 2, 59-27 pattern).

Root cause #2 of epic 105: the movement classification prompt defines movement
as relocation "between dungeon regions", and the router's state summary never
mentions that the current region's one onward exit is a descent named "Down the
Rope" (a registered seam route). The router was asked to recognize a descent it
was never told existed. This projection is the lexical bridge: it surfaces the
PC's current cartography region's REAL exits (adjacency neighbors + seam routes)
into the state summary the router sees, and emits an OTEL span when it fires.
"""

from __future__ import annotations

import types

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
)
from sidequest.server.intent_router_pass import _build_state_summary

# ---------------------------------------------------------------------------
# Cartography helpers — beneath_sunden-shaped (mirrors Task 3 fixtures).
# ---------------------------------------------------------------------------


def _hybrid_cartography() -> CartographyConfig:
    """Region-mode world with a registered seam route AND adjacency.

    ``the_dropmouth`` owns the ``Down the Rope`` route to ``deep_descent`` (a
    registered seam kind) and is adjacent to ``ropefoot``; ``ropefoot`` is
    adjacent back to ``the_dropmouth`` (so the "adjacent" kind is testable).
    """
    return CartographyConfig(
        starting_region="ropefoot",
        navigation_mode=NavigationMode.region,
        regions={
            "ropefoot": Region(
                name="Ropefoot",
                summary="Surface camp.",
                description="The waiting camp above the shaft.",
                adjacent=["the_dropmouth"],
            ),
            "the_dropmouth": Region(
                name="The Dropmouth",
                summary="The lip of the shaft.",
                description="The mouth of the descent.",
                adjacent=["ropefoot"],
            ),
        },
        routes=[
            Route(
                name="Down the Rope",
                description="The one-way descent.",
                from_id="the_dropmouth",
                to_id="deep_descent",
            ),
        ],
    )


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    """Duck-typed GenrePack exposing only what _build_state_summary reads.

    ``rules=None`` skips the confrontation block; ``witnessed_acts=None`` skips
    the witnessed-act block; ``worlds[slug].cartography`` feeds the new
    region-exits projection.
    """
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(
        rules=None,
        witnessed_acts=None,
        worlds={world_slug: world},
    )


def _snapshot(region: str) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Groucho": region},
        player_seats={"p1": "Groucho"},
    )


class _Kit:
    def __init__(self, snapshot, pack):
        self.snapshot = snapshot
        self.pack = pack


# ---------------------------------------------------------------------------
# Dungeon doubles (pingpong 2026-06-12: PC standing ON the dungeon graph).
# Mirrors tests/agents/subsystems/test_movement_seam_crossing.py.
# ---------------------------------------------------------------------------


class _StoreWithDeepGraph:
    """DungeonStore double: entrance + one deep region + one HIDDEN side passage."""

    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(
            RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar", depth_score=0.0)
        )
        g.add_node(
            RegionNode(id="exp001.r0", expansion_id=1, theme="shaft_collar", depth_score=7.9)
        )
        g.add_node(
            RegionNode(id="exp001.r1", expansion_id=1, theme="shaft_collar", depth_score=9.1)
        )
        g.add_edge(RegionEdge(a=entrance_id, b="exp001.r0", kind="shaft"))
        g.add_edge(RegionEdge(a=entrance_id, b="exp001.r1", kind="secret", hidden=True))
        return g


class _FakePalette:
    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


@pytest.fixture
def hybrid_world_kit():
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    return _Kit(_snapshot("the_dropmouth"), pack)


@pytest.fixture
def hybrid_world_kit_at_ropefoot():
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    return _Kit(_snapshot("ropefoot"), pack)


@pytest.fixture
def plain_snapshot_and_pack():
    """A pack/world with no cartography at all → no projection."""
    snap = _snapshot("nowhere")
    world = types.SimpleNamespace(cartography=None)
    pack = types.SimpleNamespace(rules=None, witnessed_acts=None, worlds={"beneath_sunden": world})
    return snap, pack


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_seam_region_summary_names_the_exit(hybrid_world_kit):
    kit = hybrid_world_kit  # PC at the_dropmouth
    summary = _build_state_summary(kit.snapshot, pack=kit.pack)
    exits = summary["current_region_exits"]
    assert {"name": "Down the Rope", "kind": "seam"} in exits


def test_adjacent_regions_listed(hybrid_world_kit_at_ropefoot):
    kit = hybrid_world_kit_at_ropefoot
    summary = _build_state_summary(kit.snapshot, pack=kit.pack)
    kinds = {(e["name"], e["kind"]) for e in summary["current_region_exits"]}
    assert ("The Dropmouth", "adjacent") in kinds  # cart region's display name


def test_no_cartography_no_projection(plain_snapshot_and_pack):
    snapshot, pack = plain_snapshot_and_pack
    summary = _build_state_summary(snapshot, pack=pack)
    assert "current_region_exits" not in summary


def test_split_party_omits_projection(caplog):
    """Disagreeing pc_regions → region_for() is None → projection OMITTED.

    Pins the behavior the original comment misdescribed: the router gets no
    exit vocabulary on a split party, and the skip is logged so the GM panel
    can distinguish it from "no cartography".
    """
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Groucho": "the_dropmouth", "Harpo": "ropefoot"},
        player_seats={"p1": "Groucho", "p2": "Harpo"},
    )
    with caplog.at_level("WARNING", logger="sidequest.server.intent_router_pass"):
        summary = _build_state_summary(snap, pack=pack)
    assert "current_region_exits" not in summary
    assert any(
        "intent_router.region_exits projection_skipped" in r.getMessage() for r in caplog.records
    )


def test_no_pack_no_projection():
    snap = _snapshot("the_dropmouth")
    summary = _build_state_summary(snap)  # pack=None
    assert "current_region_exits" not in summary


def test_dungeon_node_pc_projects_graph_exits(hybrid_world_kit):
    """Pingpong 2026-06-12: a PC whose region is a dungeon graph node (post
    seam-crossing) must get the DUNGEON exits as the lexical bridge — the
    silent skip here is why descent intents never classified as movement."""
    kit = hybrid_world_kit
    kit.snapshot.pc_regions["Groucho"] = ENTRANCE_ID
    summary = _build_state_summary(
        kit.snapshot, pack=kit.pack, dungeon_store=_StoreWithDeepGraph(), palette=_FakePalette()
    )
    exits = summary["current_region_exits"]
    assert {"name": "exp001.r0", "kind": "shaft"} in exits


def test_dungeon_hidden_exit_omitted_unless_discovered(hybrid_world_kit):
    """Secret edges are never volunteered (reverse-Illusionism) unless the
    route is already in snapshot.discovered_routes."""
    kit = hybrid_world_kit
    kit.snapshot.pc_regions["Groucho"] = ENTRANCE_ID
    summary = _build_state_summary(
        kit.snapshot, pack=kit.pack, dungeon_store=_StoreWithDeepGraph(), palette=_FakePalette()
    )
    names = {e["name"] for e in summary["current_region_exits"]}
    assert "exp001.r1" not in names

    kit.snapshot.discovered_routes.append("exp001.r1")
    summary = _build_state_summary(
        kit.snapshot, pack=kit.pack, dungeon_store=_StoreWithDeepGraph(), palette=_FakePalette()
    )
    names = {e["name"] for e in summary["current_region_exits"]}
    assert "exp001.r1" in names


def test_dungeon_node_pc_without_store_logs_skip(caplog, hybrid_world_kit):
    """A PC region in neither cartography nor a reachable dungeon graph is an
    unmapped position — skip LOUDLY (the old silent skip is the bug).

    Story 105-3: the ENTRANCE node is no longer such a position — its ascent
    seam back to the surface owner is derivable from cartography ALONE (no
    dungeon store needed), so an entrance with no store now projects that one
    exit rather than warning. The genuinely-unmapped case this test pins is a
    DEEP graph node (``exp001.r1``) reached with no store to resolve it.
    """
    kit = hybrid_world_kit
    kit.snapshot.pc_regions["Groucho"] = "exp001.r1"
    with caplog.at_level("WARNING", logger="sidequest.server.intent_router_pass"):
        summary = _build_state_summary(kit.snapshot, pack=kit.pack)
    assert "current_region_exits" not in summary
    assert any(
        "intent_router.region_exits projection_skipped" in r.getMessage() for r in caplog.records
    )


def test_dungeon_projection_emits_span(otel_capture, hybrid_world_kit):
    kit = hybrid_world_kit
    kit.snapshot.pc_regions["Groucho"] = ENTRANCE_ID
    _build_state_summary(
        kit.snapshot, pack=kit.pack, dungeon_store=_StoreWithDeepGraph(), palette=_FakePalette()
    )
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "intent_router.region_exits"]
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    # 1 visible graph edge (the hidden 'secret' side passage excluded) + 1
    # cartography-derived ascent seam back UP to the surface owner (Story
    # 105-3: the entrance's onward vocabulary includes the way out).
    assert attrs.get("exit_count") == 2
    assert attrs.get("seam_count") == 1
    assert attrs.get("region_id") == ENTRANCE_ID


def test_projection_emits_span(otel_capture, hybrid_world_kit):
    kit = hybrid_world_kit
    _build_state_summary(kit.snapshot, pack=kit.pack)
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "intent_router.region_exits"]
    assert len(spans) == 1, "expected exactly one region_exits span"
    attrs = spans[0].attributes or {}
    # the_dropmouth: one adjacent (ropefoot) + one seam (Down the Rope)
    assert attrs.get("exit_count") == 2
    assert attrs.get("seam_count") == 1
    assert attrs.get("region_id") == "the_dropmouth"
    assert attrs.get("genre_slug") == "caverns_and_claudes"
