"""Reverse seam — leaving the Deep: entrance→surface ascent (Story 105-3).

The mirror of test_movement_seam_crossing.py. 105-2 wired the surface→deep
crossing (a PC at ``the_dropmouth`` descending binds to the dungeon entrance
node). 105-3 wires the RETURN: a PC standing on the dungeon entrance node who
intends back/up/toward_exit with NO deeper in-graph candidate edge resolves to
the cartography region that OWNS the seam route (``seam_route_for`` from_id, i.e.
``the_dropmouth``) via the same per-PC ``pc_region`` patch path — never narrator
improvisation.

Span contract (story-authoritative): the crossing is the SAME bidirectional seam
route, so ``movement.resolved`` carries ``seam_kind="deep_descent"`` (unchanged)
and the new direction discriminator ``resolved_via="surface_ascent"``.

These tests are RED until 105-3 lands. Today the entrance-node back intent
fail-louds via ``movement.unresolved`` (no_candidate_edges), which is honest but
strands the party below.
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
from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
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
        idempotency_key="mv-ascent",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


def _exit() -> SubsystemDispatch:
    """Router exit_site shape (Story 164-3, Task 5): leaving the site the party
    is inside — the cutover's replacement for the direction-driven ascent."""
    return SubsystemDispatch(
        subsystem="movement",
        params={"action": "exit_site"},
        idempotency_key="mv-ascent-exit",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


# ---------------------------------------------------------------------------
# Store doubles (content-free) — same shape as test_movement_seam_crossing.py.
# ---------------------------------------------------------------------------


class _StoreWithEntrance:
    """DungeonStore double: load_map returns a graph with ONLY the entrance node.

    No in-graph edges, so an exit-ward intent at the entrance has no in-graph
    candidate — the case the reverse seam must catch.
    """

    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar"))
        return g


class _FakePalette:
    """Duck-typed ThemePalette — the ascent doesn't reach projection, but we
    supply one to keep the signature valid (mirrors the descent suite)."""

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

    ``the_dropmouth`` owns the one-way descent route to ``deep_descent`` (a
    registered seam kind). The seam is bidirectional at the threshold: the same
    route is the surface owner a PC at the entrance node ascends back to.
    """
    return CartographyConfig(
        starting_region="ropefoot",
        navigation_mode=NavigationMode.region,
        regions={
            "ropefoot": Region(
                name="Ropefoot",
                summary="Surface camp.",
                description="The waiting camp above the shaft.",
            ),
            "the_dropmouth": Region(
                name="The Dropmouth",
                summary="The lip of the shaft.",
                description="The mouth of the descent.",
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


def _null_from_id_cartography() -> CartographyConfig:
    """Malformed seam: a registered-kind (``deep_descent``) route whose
    ``from_id`` is null — the exact homebrew fat-finger the reviewer's Devil's
    Advocate flagged (Jade authors packs now; ``Route.from_id`` is
    ``str | None = None`` with NO validator, so this loads and plays fine on the
    surface and on descent — the bomb only goes off on the way back up).

    ``surface_owner_for_entrance`` returns this route (its distinct-from_id set
    is ``{None}``, length 1), so the ascent branch reaches ``resolve_surface_ascent``
    with a null surface owner.
    """
    return CartographyConfig(
        starting_region="the_dropmouth",
        navigation_mode=NavigationMode.region,
        regions={
            "the_dropmouth": Region(
                name="The Dropmouth",
                summary="The lip of the shaft.",
                description="The mouth of the descent.",
            ),
        },
        routes=[
            Route(
                name="Down the Rope",
                description="A descent whose surface owner was never wired.",
                from_id=None,  # the wiring fault
                to_id="deep_descent",
            ),
        ],
    )


def _dangling_from_id_cartography() -> CartographyConfig:
    """Malformed seam: the registered-kind route's ``from_id`` names a surface
    region that does NOT exist in ``cartography.regions`` (a typo'd id). The
    descent guards its target (``entrance_id in graph.nodes``); the ascent must
    guard symmetrically, or it binds the PC to a phantom region.
    """
    return CartographyConfig(
        starting_region="the_dropmouth",
        navigation_mode=NavigationMode.region,
        regions={
            "the_dropmouth": Region(
                name="The Dropmouth",
                summary="The lip of the shaft.",
                description="The mouth of the descent.",
            ),
        },
        routes=[
            Route(
                name="Down the Rope",
                description="A descent that returns to a region that isn't mapped.",
                from_id="ghost_dropmouth",  # NOT present in regions
                to_id="deep_descent",
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
    local = provider.get_tracer("test-movement-surface-ascent")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


@pytest.fixture
def deep_world_kit():
    """PC ``Groucho`` is DEEP — seated on the dungeon entrance node."""
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = _snapshot({"Groucho": ENTRANCE_ID}, {"p1": "Groucho"})
    return _HybridKit(snap, pack, _StoreWithEntrance(), _FakePalette())


@pytest.fixture
def deep_oz_kit():
    """PC ``Dorothy`` is deep at the entrance node of a NO-seam world."""
    cart = _oz_cartography()
    pack = _pack_with_cartography("oz", cart)
    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        pc_regions={"Dorothy": ENTRANCE_ID},
        player_seats={"p1": "Dorothy"},
    )
    return _HybridKit(snap, pack, _StoreWithEntrance(), _FakePalette())


@pytest.fixture
def deep_null_owner_kit():
    """PC ``Groucho`` is deep, but the seam route has a null ``from_id``."""
    cart = _null_from_id_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = _snapshot({"Groucho": ENTRANCE_ID}, {"p1": "Groucho"})
    return _HybridKit(snap, pack, _StoreWithEntrance(), _FakePalette())


@pytest.fixture
def deep_dangling_owner_kit():
    """PC ``Groucho`` is deep; the seam route's ``from_id`` names an unmapped region."""
    cart = _dangling_from_id_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = _snapshot({"Groucho": ENTRANCE_ID}, {"p1": "Groucho"})
    return _HybridKit(snap, pack, _StoreWithEntrance(), _FakePalette())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_entrance_node_ascends_to_surface(capture_spans, deep_world_kit):
    """AC1 + AC2 (retargeted): from the legacy frontier entrance node, an
    ``exit_site`` binds the PC back to the site's owning cartography region
    (the_dropmouth, the site's ``attached_to``) via the per-PC patch path — no
    improvisation. The legacy un-namespaced ``entrance`` is recognized as site
    membership through the ``is_procedural_region_id`` shim. Was surface_ascent;
    the destination is unchanged."""
    kit = deep_world_kit
    out = _run(
        run_movement_dispatch(
            _exit(),
            snapshot=kit.snapshot,
            player_name="Groucho",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data.get("resolved_via") == "site_exit", (
        f"expected site_exit crossing, got: {out.data}"
    )
    assert out.data.get("to_region") == "the_dropmouth", (
        f"expected to_region='the_dropmouth' (the site owner), got: {out.data.get('to_region')!r}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == "the_dropmouth", (
        f"PC not rebound to surface; still at {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    # OTEL proof the ascent was the site resolver, not improvisation: the
    # site.exit span carries the surface owner as the destination.
    exit_spans = [s for s in capture_spans.get_finished_spans() if s.name == "site.exit"]
    assert len(exit_spans) == 1, "expected exactly one site.exit span for the ascent"
    attrs = exit_spans[0].attributes or {}
    assert attrs.get("resolved_via") == "site_exit"
    assert attrs.get("to_region") == "the_dropmouth"
    assert attrs.get("site_id") == "frontier"


def test_deeper_from_entrance_does_not_ascend(capture_spans, deep_world_kit):
    """AC4 discrimination: ``deeper`` at the entrance with no in-graph candidate
    must NOT hijack into an ascent — the reverse seam is for exit-ward intents
    only. The party can't go deeper (no node), so this stays unresolved."""
    kit = deep_world_kit
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
    assert out.data.get("resolved_via") != "surface_ascent", (
        f"'deeper' must not trigger an ascent, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        "a non-ascending intent must not move the PC off the entrance node"
    )


def test_no_seam_world_does_not_invent_surface(capture_spans, deep_oz_kit):
    """No silent fallback: in a region-mode world with NO registered seam route,
    a back intent at the entrance must NOT resolve to a fabricated surface region
    via ascent. The ascent fires only when a real seam route owns the crossing."""
    kit = deep_oz_kit
    out = _run(
        run_movement_dispatch(
            _movement("back"),
            snapshot=kit.snapshot,
            player_name="Dorothy",
            dungeon_store=kit.store,
            palette=kit.palette,
            pack=kit.pack,
        )
    )
    assert out.data.get("resolved_via") != "surface_ascent", (
        f"no-seam world must not ascend via the seam path, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Dorothy") == ENTRANCE_ID, (
        "PC must not be moved to an invented surface region"
    )


# ---------------------------------------------------------------------------
# Reviewer rework (REJECTED 2026-06-13): the error geometry is asymmetric with
# the descent it claims to mirror. A registered-kind (deep_descent) route IS a
# seam — a MALFORMED one is a wiring fault that must fail LOUD through
# movement.unresolved (the OTEL lie-detector), never an uncaught raise and never
# a silent region_mode defer (which would hand a confabulated 'way up' to the
# narrator — the exact "convincing narration, zero mechanical backing" the OTEL
# principle exists to catch). These pin findings #1 (null from_id) and #3
# (dangling from_id). Finding #2 (pyright str|None) is a static-type fix covered
# by `uv run pyright`, not a runtime behavior — no test here.
# ---------------------------------------------------------------------------


def test_null_from_id_seam_fails_loud_not_raises(capture_spans, deep_null_owner_kit):
    """Reviewer finding #1 (HIGH): a registered-kind (``deep_descent``) route with
    a null ``from_id`` must NOT crash the turn.

    ``surface_owner_for_entrance`` returns it (its distinct-from_id set is
    ``{None}``, length 1), and today ``resolve_surface_ascent`` raises
    ``SeamCrossingError(no_surface_owner)`` uncaught out of
    ``run_movement_dispatch`` — skipping the mandated span and blinding the GM
    panel. The descent twin wraps its resolver in ``try/except SeamCrossingError
    → _unresolved``; the ascent must too. The malformed seam fails LOUD via
    ``movement.unresolved``, never an uncaught raise, never a silent defer.
    """
    kit = deep_null_owner_kit
    # The bug IS the uncaught SeamCrossingError on this call — it must not raise.
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
    # PC never bound to a null/phantom surface — stays put on the entrance node.
    assert out.data.get("resolved_via") != "surface_ascent", (
        f"a null-from_id seam must not complete an ascent, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        "a malformed seam must not move the PC off the entrance node; still at "
        f"{kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    # Fail LOUD: a registered-kind route IS a seam, so a malformed one is a
    # wiring fault the GM panel must see — movement.unresolved, NOT a silent
    # region_mode defer.
    span_names = [s.name for s in capture_spans.get_finished_spans()]
    unresolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.unresolved"]
    assert len(unresolved) == 1, (
        f"expected exactly one movement.unresolved span for the malformed seam; "
        f"spans seen: {span_names}"
    )
    # And no false movement.resolved span (no PC was actually moved).
    assert "movement.resolved" not in span_names, (
        "a malformed seam must not emit a movement.resolved span"
    )


def test_dangling_from_id_does_not_bind_phantom_region(capture_spans, deep_dangling_owner_kit):
    """Reviewer finding #3 (MEDIUM): the ascent binds the PC to ``route.from_id``
    without verifying it names a real cartography region.

    The descent guards its target (``entrance_id in graph.nodes``) and fails loud
    otherwise; the ascent must guard symmetrically. A route whose ``from_id`` is a
    typo'd / unmapped id must NOT strand the PC in a phantom region — it fails
    loud via ``movement.unresolved``.
    """
    kit = deep_dangling_owner_kit
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
    assert out.data.get("resolved_via") != "surface_ascent", (
        f"ascent must not bind to an unmapped surface region, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        "PC bound to a phantom region "
        f"{kit.snapshot.region_for(perspective='Groucho')!r}; the resolved surface "
        "id must exist in cartography.regions"
    )
    span_names = [s.name for s in capture_spans.get_finished_spans()]
    unresolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.unresolved"]
    assert len(unresolved) == 1, (
        f"expected movement.unresolved for the dangling surface id; spans seen: {span_names}"
    )
