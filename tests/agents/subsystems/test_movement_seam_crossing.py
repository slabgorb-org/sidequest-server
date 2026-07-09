"""Hybrid-world movement: region-mode + seam route crosses; seam-less defers.

Story 105-2 AC1 + AC5. Root cause #1: de4f85c8's region_mode_deferred
early-return dead-coded the 59-12 handoff for beneath_sunden. These tests
pin the fix: a region-mode world WITH a seam route + live store crosses;
oz-shaped worlds (no seam route) defer exactly as before.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
    SiteDecl,
)
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _movement(direction: str, descriptor: str = "") -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"direction": direction, "exit_descriptor": descriptor},
        idempotency_key="mv-seam",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


# ---------------------------------------------------------------------------
# Store doubles (content-free) — same shape as test_seam_deep_descent.py.
# ---------------------------------------------------------------------------


class _StoreWithEntrance:
    """DungeonRepository double modeling the LEGACY Sünden frontier store: its
    graph is keyed on the bare ``ENTRANCE_ID`` (not the site-namespaced
    ``frontier:entrance`` the descriptor declares), so ``resolve_enter_site``'s
    entrance fallback binds the PC to ``entrance``. Accepts the ``site_id`` the
    resolver threads (and the site-less ``entrance_id``-only ``_in_dungeon``
    probe call)."""

    def load_map(self, *, entrance_id, site_id: str = "frontier"):
        g = RegionGraph(entrance_id=ENTRANCE_ID)
        g.add_node(RegionNode(id=ENTRANCE_ID, expansion_id=0, theme="shaft_collar"))
        return g


class _EmptyStore:
    """DungeonRepository double: load_map returns a graph with NO nodes (corrupt
    seed). Still accepts the ``site_id`` the resolver threads — the enter
    resolver then fails loud on ``no_site_entrance`` (no declared node AND no
    graph entrance node to fall back to)."""

    def load_map(self, *, entrance_id, site_id: str = "frontier"):
        return RegionGraph(entrance_id=entrance_id)


class _StoreWithDeepGraph:
    """Legacy frontier store + one materialized deep region below the entrance.

    The post-crossing shape of the live 2026-06-12 session (pingpong): the PC
    stands ON the dungeon graph (pc_regions == 'entrance') in a region-mode
    world; the deep is materialized and adjacent. In-dungeon movement must
    traverse THIS graph, not defer to the narrator. Graph keyed on the bare
    ``ENTRANCE_ID`` (legacy shape); accepts ``site_id``.
    """

    def load_map(self, *, entrance_id, site_id: str = "frontier"):
        g = RegionGraph(entrance_id=ENTRANCE_ID)
        g.add_node(
            RegionNode(id=ENTRANCE_ID, expansion_id=0, theme="shaft_collar", depth_score=0.0)
        )
        g.add_node(
            RegionNode(id="exp001.r0", expansion_id=1, theme="shaft_collar", depth_score=7.9)
        )
        g.add_edge(RegionEdge(a=ENTRANCE_ID, b="exp001.r0", kind="shaft"))
        return g


class _FakePalette:
    """Duck-typed ThemePalette — movement handler doesn't reach projection for
    surface→deep crossings, but we supply one to keep the signature valid."""

    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


# ---------------------------------------------------------------------------
# Cartography helpers
# ---------------------------------------------------------------------------


def _hybrid_cartography() -> CartographyConfig:
    """beneath_sunden-shaped: region-mode with a declared ``frontier`` site.

    ``the_dropmouth`` OWNS the site (``attached_to``); ``ropefoot`` (the surface
    camp) is one step adjacent to it. A descent (``direction=="deeper"``) from
    EITHER crosses into the frontier via the SiteRegistry × ``enter_site``
    resolver — the retired owned/adjacent seam rungs.
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
        sites=[
            SiteDecl(
                site_id="frontier",
                name="The Deep",
                archetype="megadungeon",
                attached_to="the_dropmouth",
                extent="frontier",
            ),
        ],
    )


def _oz_cartography() -> CartographyConfig:
    """wry_whimsy/oz-shaped: region-mode with NO registered seam routes."""
    return CartographyConfig(
        starting_region="munchkin_country",
        navigation_mode=NavigationMode.region,
        regions={
            "munchkin_country": Region(
                name="Munchkin Country",
                summary="The land of the Munchkins.",
                description="A cheerful pastoral region.",
            ),
        },
        routes=[
            Route(
                name="Yellow Brick Road",
                description="The road to the Emerald City.",
                from_id="munchkin_country",
                to_id="emerald_city",  # NOT a registered seam kind
            ),
        ],
    )


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    """Duck-typed GenrePack: exposes pack.worlds[slug].cartography."""
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(worlds={world_slug: world})


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _snapshot(pc_regions: dict[str, str], seats: dict[str, str]) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions=dict(pc_regions),
        player_seats=dict(seats),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _HybridKit:
    """All the moving parts for a hybrid-world (region-mode + seam) test."""

    def __init__(self, snapshot, pack, store, palette):
        self.snapshot = snapshot
        self.pack = pack
        self.store = store
        self.palette = palette


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-movement-seam-crossing")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


@pytest.fixture
def hybrid_world_kit():
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = _snapshot({"Groucho": "the_dropmouth"}, {"p1": "Groucho"})
    return _HybridKit(snap, pack, _StoreWithEntrance(), _FakePalette())


@pytest.fixture
def hybrid_world_kit_empty_store():
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = _snapshot({"Groucho": "the_dropmouth"}, {"p1": "Groucho"})
    return _HybridKit(snap, pack, _EmptyStore(), _FakePalette())


@pytest.fixture
def in_dungeon_kit():
    """Region-mode hybrid world with the PC ALREADY inside the dungeon graph."""
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = _snapshot({"Groucho": ENTRANCE_ID}, {"p1": "Groucho"})
    snap.discovered_regions.append(ENTRANCE_ID)
    return _HybridKit(snap, pack, _StoreWithDeepGraph(), _FakePalette())


@pytest.fixture
def surface_adjacent_kit():
    """Region-mode hybrid world with the PC on the surface CAMP (ropefoot),
    one step from the seam-owner (the_dropmouth). The sq-playtest 2026-06-21
    repro: the party starts at ropefoot, never the_dropmouth, so the descent
    must cross from one step off the seam."""
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = _snapshot({"Groucho": "ropefoot"}, {"p1": "Groucho"})
    return _HybridKit(snap, pack, _StoreWithEntrance(), _FakePalette())


@pytest.fixture
def oz_shaped_kit():
    cart = _oz_cartography()
    pack = _pack_with_cartography("oz", cart)
    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        pc_regions={"Dorothy": "munchkin_country"},
        player_seats={"p1": "Dorothy"},
    )
    return _HybridKit(snap, pack, None, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "direction,descriptor",
    [
        # Only ``direction=="deeper"`` (or ``action=="enter_site"``) crosses under
        # the site model — the retired ladder's "any non-back intent crosses"
        # breadth is gone. Every case here descends; the descriptor is the WAY
        # (ignored by the deeper path, which resolves the sole enterable site).
        ("deeper", ""),
        ("deeper", "down into the deep"),
        ("deeper", "down the rope"),
        ("deeper", "follow the rope down"),
    ],
)
def test_seam_region_movement_crosses_to_entrance(
    capture_spans, hybrid_world_kit, direction, descriptor
):
    """AC1: a region-mode world with a declared site + live store crosses (not defers)."""
    kit = hybrid_world_kit
    out = _run(
        run_movement_dispatch(
            _movement(direction, descriptor),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data["resolved_via"] == "site_enter", (
        f"expected site_enter crossing, got: {out.data}"
    )
    assert out.data["to_region"] == ENTRANCE_ID, (
        f"expected to_region={ENTRANCE_ID!r}, got: {out.data.get('to_region')!r}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        f"PC not rebound to entrance; still at {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    # OTEL proof the crossing was the site resolver, not improvisation: a single
    # site.enter span whose resolved_via attr names the enter_site resolver.
    enters = [s for s in capture_spans.get_finished_spans() if s.name == "site.enter"]
    assert len(enters) == 1, "expected exactly one site.enter span for the crossing"
    assert (enters[0].attributes or {})["resolved_via"] == "site_enter"


def test_surface_adjacent_descent_crosses_to_entrance(capture_spans, surface_adjacent_kit):
    """sq-playtest 2026-06-21: a PC on the surface camp (ropefoot), one step from
    the seam-owner (the_dropmouth), descends in ONE deliberate action. The party
    starts here, never on the_dropmouth — so without this the dungeon was
    unreachable (three descents, still current_region='ropefoot')."""
    kit = surface_adjacent_kit
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "down the rope"),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data["resolved_via"] == "site_enter", (
        f"expected adjacent site_enter crossing, got: {out.data}"
    )
    assert out.data["to_region"] == ENTRANCE_ID
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        f"PC not rebound to entrance; still at {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    enters = [s for s in capture_spans.get_finished_spans() if s.name == "site.enter"]
    assert len(enters) == 1, "expected exactly one site.enter span for the crossing"
    assert (enters[0].attributes or {})["resolved_via"] == "site_enter"


@pytest.mark.parametrize("direction", ["back", "toward_exit", ""])
def test_surface_adjacent_non_deeper_does_not_cross(capture_spans, surface_adjacent_kit, direction):
    """The adjacency descent is gated on direction == "deeper". Lateral or
    descriptor-only intra-camp movement ("walk to the board") must NOT teleport
    the party into the deep — it defers like any other surface region move."""
    kit = surface_adjacent_kit
    out = _run(
        run_movement_dispatch(
            _movement(direction, "over to the board"),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data["resolved_via"] == "region_mode_deferred", (
        f"non-deeper surface move must defer, not cross, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == "ropefoot", (
        "a non-descent intent must not move the PC off the camp"
    )


def test_seam_region_back_does_not_cross(capture_spans, hybrid_world_kit):
    """back is surface adjacency — the seam guard must not fire; defer as before."""
    kit = hybrid_world_kit
    out = _run(
        run_movement_dispatch(
            _movement("back"),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data["resolved_via"] == "region_mode_deferred", (
        f"back should defer (not cross), got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == "the_dropmouth", (
        "back must not move the PC"
    )


def test_non_seam_region_mode_world_still_defers(capture_spans, oz_shaped_kit):
    """AC5 non-regression: region-mode world with NO seam routes (oz/wonderland)
    defers exactly as before (de4f85c8 is unchanged for these worlds)."""
    kit = oz_shaped_kit
    out = _run(
        run_movement_dispatch(
            _movement("deeper"),
            snapshot=kit.snapshot,
            player_name="Dorothy",
            dungeon_store=None,
            palette=None,
            pack=kit.pack,
        )
    )
    assert out.data["resolved_via"] == "region_mode_deferred", (
        f"oz-shaped world must defer, got: {out.data}"
    )


def test_in_dungeon_pc_traverses_graph_not_defers(capture_spans, in_dungeon_kit):
    """Pingpong 2026-06-12 (beneath_sunden confabulated crawl): a region-mode
    world's PC standing ON a dungeon graph node must run the §Q1 procedural
    navigator — NOT region_mode_deferred (which hands the crawl to the
    narrator, who improvises it)."""
    kit = in_dungeon_kit
    out = _run(
        run_movement_dispatch(
            _movement("deeper"),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data.get("resolved_via") == "depth_delta", (
        f"in-dungeon movement must resolve via the graph navigator, got: {out.data}"
    )
    assert out.data["to_region"] == "exp001.r0"
    assert kit.snapshot.region_for(perspective="Groucho") == "exp001.r0", (
        "PC must advance to the deeper region"
    )
    resolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.resolved"]
    assert len(resolved) == 1, "expected exactly one movement.resolved span for the traversal"


def test_in_dungeon_back_traverses_toward_entrance(capture_spans, in_dungeon_kit):
    """In-dungeon ``back`` is graph traversal toward the surface, not a defer."""
    kit = in_dungeon_kit
    kit.snapshot.pc_regions["Groucho"] = "exp001.r0"
    kit.snapshot.discovered_regions.append("exp001.r0")
    out = _run(
        run_movement_dispatch(
            _movement("back"),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data.get("resolved_via") == "depth_delta", (
        f"in-dungeon back must resolve via the graph navigator, got: {out.data}"
    )
    assert out.data["to_region"] == ENTRANCE_ID
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID


def test_seam_region_with_dead_store_fails_loud(capture_spans, hybrid_world_kit_empty_store):
    """Hybrid world + declared site + CORRUPT store (no entrance node) → fail loud.

    ``resolve_enter_site`` finds neither the declared namespaced entrance nor a
    graph entrance node to fall back to, so it raises ``no_site_entrance`` (the
    site-model successor to the retired ``no_dungeon_entrance``)."""
    kit = hybrid_world_kit_empty_store
    out = _run(
        run_movement_dispatch(
            _movement("deeper"),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data.get("error") == "no_site_entrance", (
        f"dead store must fail loud with no_site_entrance, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == "the_dropmouth", (
        "PC must not move on a seam-crossing failure"
    )
