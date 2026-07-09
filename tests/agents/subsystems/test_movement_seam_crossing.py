"""Hybrid-world movement: region-mode SITE enter/exit crosses; siteless defers.

Story 105-2 AC1 + AC5, RETARGETED for the Track B Story 164-3 cutover: the
region-mode seam LADDER (direction-driven surface_descent / surface_ascent) is
replaced by SITE enter/exit dispatched BY KIND (``action=enter_site`` /
``action=exit_site``) through the SiteRegistry × the enter_site/exit_site
resolvers. The DESTINATION is unchanged — a region-mode world WITH a frontier
site + live store crosses onto the site entrance; oz-shaped worlds (no site)
defer exactly as before. Only the trigger (action, not direction) and the
``resolved_via`` name (``site_enter``/``site_exit``, spanned as ``site.enter`` /
``site.exit``, not ``movement.resolved`` + seam_kind) differ.
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


def _enter(descriptor: str = "the deep") -> SubsystemDispatch:
    """Router enter_site shape (Story 164-3, Task 5)."""
    return SubsystemDispatch(
        subsystem="movement",
        params={"action": "enter_site", "site_descriptor": descriptor},
        idempotency_key="mv-seam-enter",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


def _exit() -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"action": "exit_site"},
        idempotency_key="mv-seam-exit",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


# ---------------------------------------------------------------------------
# Store doubles (content-free) — model Sünden's frontier-legacy graph: the
# entrance is the un-namespaced legacy ENTRANCE_ID, and load_map accepts the
# site-keyed resolver signature.
# ---------------------------------------------------------------------------


class _StoreWithEntrance:
    """DungeonStore/Repository double: graph anchored on the legacy ENTRANCE_ID
    (Sünden frontier keeps un-namespaced node ids for B1 — resolve_enter_site
    binds to graph.entrance_id via the frontier-legacy fallback)."""

    def load_map(self, *, entrance_id=ENTRANCE_ID, site_id="frontier"):
        g = RegionGraph(entrance_id=ENTRANCE_ID)
        g.add_node(RegionNode(id=ENTRANCE_ID, expansion_id=0, theme="shaft_collar"))
        return g


class _EmptyStore:
    """DungeonStore double: load_map returns a graph with NO nodes (corrupt seed)."""

    def load_map(self, *, entrance_id=ENTRANCE_ID, site_id="frontier"):
        return RegionGraph(entrance_id=entrance_id)


class _StoreWithDeepGraph:
    """DungeonStore double: entrance + one materialized deep region below it.

    The post-crossing shape of the live 2026-06-12 session (pingpong): the PC
    stands ON the dungeon graph (pc_regions == 'entrance') in a region-mode
    world; the deep is materialized and adjacent. In-dungeon movement must
    traverse THIS graph, not defer to the narrator.
    """

    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(
            RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar", depth_score=0.0)
        )
        g.add_node(
            RegionNode(id="exp001.r0", expansion_id=1, theme="shaft_collar", depth_score=7.9)
        )
        g.add_edge(RegionEdge(a=entrance_id, b="exp001.r0", kind="shaft"))
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
    """beneath_sunden-shaped: region-mode with a registered seam route.

    the_dropmouth owns a route to ``deep_descent`` (a registered seam kind),
    so a PC there descending should cross the seam.
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
            )
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


# The descriptor must reference the SITE ("The Deep") — resolve_descriptor
# substring-matches the site name (164-1), so seam-route phrasings like "down the
# rope" no longer resolve (that's the router's job to phrase site-ward; see the
# descriptor-matching Delivery Finding). An empty descriptor also resolves when
# there is a single enterable site.
@pytest.mark.parametrize("descriptor", ["the deep", "the Deep", "down into the deep", ""])
def test_seam_region_movement_crosses_to_entrance(capture_spans, hybrid_world_kit, descriptor):
    """AC1 (retargeted): a region-mode world with a frontier site + live store
    crosses onto the site entrance via ``enter_site`` (not defers). The
    destination (ENTRANCE_ID, via the frontier-legacy graph.entrance_id fallback)
    is unchanged; the trigger is ``action=enter_site`` and the observability is
    the ``site.enter`` span."""
    kit = hybrid_world_kit
    out = _run(
        run_movement_dispatch(
            _enter(descriptor),
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
    # OTEL proof the crossing was the site resolver, not improvisation: the
    # site.enter span carries the destination + resolution.
    enter = [s for s in capture_spans.get_finished_spans() if s.name == "site.enter"]
    assert len(enter) == 1, "expected exactly one site.enter span for the crossing"
    attrs = enter[0].attributes or {}
    assert attrs["site_id"] == "frontier"
    assert attrs["to_region"] == ENTRANCE_ID


def test_surface_adjacent_descent_crosses_to_entrance(capture_spans, surface_adjacent_kit):
    """sq-playtest 2026-06-21 (retargeted): a PC on the surface camp (ropefoot),
    one step from the site owner (the_dropmouth), enters in ONE action. The party
    starts here, never on the_dropmouth — SiteRegistry.sites_for_node surfaces the
    site via adjacency, so ``enter_site`` resolves it (was surface_descent_adjacent)."""
    kit = surface_adjacent_kit
    out = _run(
        run_movement_dispatch(
            _enter("the deep"),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data["resolved_via"] == "site_enter", (
        f"expected site_enter crossing from the adjacent camp, got: {out.data}"
    )
    assert out.data["to_region"] == ENTRANCE_ID
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        f"PC not rebound to entrance; still at {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    enter = [s for s in capture_spans.get_finished_spans() if s.name == "site.enter"]
    assert len(enter) == 1, "expected exactly one site.enter span for the crossing"
    assert (enter[0].attributes or {})["site_id"] == "frontier"


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
    """Hybrid world + frontier site + CORRUPT store (no entrance node) → fail loud.

    Retargeted: an ``enter_site`` whose store yields no entrance (and no usable
    graph.entrance_id) raises ``no_site_entrance`` from resolve_enter_site; the
    movement catcher owns the failure span (site.enter_unresolved) and surfaces
    the truth through movement.unresolved. The PC does not move."""
    kit = hybrid_world_kit_empty_store
    out = _run(
        run_movement_dispatch(
            _enter("the deep"),
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
        "PC must not move on a site-crossing failure"
    )
    # The catcher owns the failure span — the GM panel sees the fail-loud.
    unresolved = [
        s for s in capture_spans.get_finished_spans() if s.name == "site.enter_unresolved"
    ]
    assert len(unresolved) == 1, "the movement catcher must emit site.enter_unresolved"
    assert (unresolved[0].attributes or {})["reason"] == "no_site_entrance"
