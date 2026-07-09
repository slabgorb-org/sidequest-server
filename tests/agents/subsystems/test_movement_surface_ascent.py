"""Reverse crossing — leaving the Deep: entrance→surface ASCENT, migrated to the
SITE model (Story 105-3 → Task 6, Story 164-3).

105-2 wired the surface→deep crossing; 105-3 wired the RETURN. Task 6 REMOVED the
five-rung inlined seam ladder and REPLACED it with SITE crossings (SiteRegistry ×
enter_site/exit_site resolvers). The ascent is now the site EXIT crossing: a PC
standing on the site's (legacy) ``entrance`` node with a non-``deeper`` intent
resolves to the region that OWNS the site (``site.attached_to`` == ``the_dropmouth``)
via ``resolve_exit_site`` and the same per-PC ``pc_region`` patch path — never
narrator improvisation. Membership of the un-namespaced ``entrance`` node is
detected via the legacy ``is_procedural_region_id`` shim → the default frontier
site, so the DESTINATION (``to_region``) is UNCHANGED from the ladder it replaces.

Span contract: a solo acting-PC crossing now emits a ``site.exit`` span (name
``site.exit``, attrs incl. ``resolved_via="site_exit"``, ``site_id="frontier"``,
``to_region``) — there is NO ``movement.resolved`` span and NO ``seam_kind`` for a
solo site crossing.

The malformed-authoring guards (a fat-fingered site owner) retarget from the
retired ``surface_owner_for_entrance`` route machinery to ``resolve_exit_site``
raising ``SeamCrossingError(dangling_site_owner)`` — caught in
``run_movement_dispatch`` (never an uncaught raise) and surfaced loud through
``movement.unresolved``; the PC is never bound to a null/phantom surface region.
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


# ---------------------------------------------------------------------------
# Store doubles (content-free) — same shape as test_movement_seam_crossing.py.
# ---------------------------------------------------------------------------


class _StoreWithEntrance:
    """DungeonRepository double modeling the LEGACY Sünden frontier store: its
    graph is keyed on the bare ``ENTRANCE_ID`` (not the site-namespaced
    ``frontier:entrance`` the descriptor declares), so ``site_owning_node`` misses
    and the ``is_procedural_region_id`` shim binds the frontier site. ``load_map``
    accepts the ``site_id`` the resolver threads. Only the entrance node exists —
    no in-graph edges, so a deeper intent at the entrance has no in-graph
    candidate.
    """

    def load_map(self, *, entrance_id: str, site_id: str = "frontier") -> RegionGraph:
        g = RegionGraph(entrance_id=ENTRANCE_ID)
        g.add_node(RegionNode(id=ENTRANCE_ID, expansion_id=0, theme="shaft_collar"))
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
    """beneath_sunden-shaped: region-mode declaring a ``frontier`` SITE.

    ``the_dropmouth`` OWNS the site; ``ropefoot`` (the surface camp) is one step
    adjacent to it. Standing on the site's (legacy) ``entrance`` node, a
    non-``deeper`` intent EXITS back to the owning region (``the_dropmouth``). The
    inert legacy ``deep_descent`` route rides along for shape-fidelity — the site
    registry, not the route, drives the crossing now.
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


def _null_owner_site_cartography() -> CartographyConfig:
    """Malformed SITE: a ``frontier`` site whose ``attached_to`` is EMPTY — the
    site-model analog of the retired null-``from_id`` seam (the exact homebrew
    fat-finger the reviewer's Devil's Advocate flagged; Jade authors packs now,
    and ``SiteDecl.attached_to`` is a bare ``str`` with no non-empty validator, so
    this loads and plays fine until the way back up).

    Standing on the site's legacy ``entrance`` node, the exit branch reaches
    ``resolve_exit_site`` with an empty owner, which fails loud
    (``dangling_site_owner``) rather than binding the PC to a null surface.
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
        sites=[
            SiteDecl(
                site_id="frontier",
                name="The Deep",
                archetype="megadungeon",
                attached_to="",  # the wiring fault: no owning region
                extent="frontier",
            ),
        ],
    )


def _dangling_owner_site_cartography() -> CartographyConfig:
    """Malformed SITE: a ``frontier`` site whose ``attached_to`` names a region
    that does NOT exist in ``cartography.regions`` (a typo'd id) — the site-model
    analog of the retired dangling-``from_id`` seam. ``resolve_exit_site`` guards
    ``attached_to in regions`` and fails loud (``dangling_site_owner``), so it
    never binds the PC to a phantom region.
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
        sites=[
            SiteDecl(
                site_id="frontier",
                name="The Deep",
                archetype="megadungeon",
                attached_to="ghost_dropmouth",  # NOT present in regions
                extent="frontier",
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
    """PC ``Groucho`` is deep; the frontier site's ``attached_to`` is EMPTY."""
    cart = _null_owner_site_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = _snapshot({"Groucho": ENTRANCE_ID}, {"p1": "Groucho"})
    return _HybridKit(snap, pack, _StoreWithEntrance(), _FakePalette())


@pytest.fixture
def deep_dangling_owner_kit():
    """PC ``Groucho`` is deep; the frontier site's ``attached_to`` names an unmapped region."""
    cart = _dangling_owner_site_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = _snapshot({"Groucho": ENTRANCE_ID}, {"p1": "Groucho"})
    return _HybridKit(snap, pack, _StoreWithEntrance(), _FakePalette())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "direction,descriptor",
    [
        ("back", ""),
        ("up", ""),
        ("toward_exit", ""),
        # Descriptor-only departure intent at the entrance still ascends.
        ("", "back up the rope"),
    ],
)
def test_entrance_node_ascends_to_surface(capture_spans, deep_world_kit, direction, descriptor):
    """AC1 + AC2 (site model): from the site's (legacy) entrance node, a
    non-``deeper`` intent EXITS to the region that OWNS the site
    (``site.attached_to`` == ``the_dropmouth``) via the per-PC patch path — no
    improvisation."""
    kit = deep_world_kit
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
    assert out.data.get("resolved_via") == "site_exit", (
        f"expected a site_exit crossing, got: {out.data}"
    )
    assert out.data.get("to_region") == "the_dropmouth", (
        f"expected to_region='the_dropmouth' (the site owner), got: {out.data.get('to_region')!r}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == "the_dropmouth", (
        f"PC not rebound to surface; still at {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    # OTEL proof the ascent was the site EXIT resolver, not improvisation. A solo
    # acting-PC crossing emits a site.exit span (there is NO movement.resolved
    # span and NO seam_kind for a solo site crossing).
    exits = [s for s in capture_spans.get_finished_spans() if s.name == "site.exit"]
    assert len(exits) == 1, "expected exactly one site.exit span for the ascent"
    attrs = exits[0].attributes or {}
    assert attrs.get("resolved_via") == "site_exit"
    assert attrs.get("site_id") == "frontier"
    assert attrs.get("to_region") == "the_dropmouth"


def test_deeper_from_entrance_does_not_ascend(capture_spans, deep_world_kit):
    """AC4 discrimination (site model): ``deeper`` at the site entrance must NOT
    hijack into a site EXIT — the exit crossing is for non-``deeper`` intents
    only. With only the entrance node materialized there is no deeper in-graph
    candidate, so this stays unresolved and the PC does not leave the site."""
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
    assert out.data.get("resolved_via") != "site_exit", (
        f"'deeper' must not trigger a site exit, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        "a non-exiting intent must not move the PC off the entrance node (not bound to surface)"
    )


def test_no_seam_world_does_not_invent_surface(capture_spans, deep_oz_kit):
    """No silent fallback: a region-mode world with NO declared site (its
    cartography carries no ``sites:`` block) must NOT exit to a fabricated surface
    region. The site EXIT fires only when a real site owns the PC's node."""
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
    assert out.data.get("resolved_via") != "site_exit", (
        f"no-site world must not exit via the site path, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Dorothy") == ENTRANCE_ID, (
        "PC must not be moved to an invented surface region"
    )


# ---------------------------------------------------------------------------
# Reviewer rework (REJECTED 2026-06-13), RETARGETED to the site model (Task 6,
# Story 164-3): the retired seam-route machinery is GONE, so a malformed
# seam-route ``from_id`` has no direct equivalent — the site-model analog is a
# malformed SITE owner (``SiteDecl.attached_to`` empty or unmapped).
# ``resolve_exit_site`` raises ``SeamCrossingError(dangling_site_owner)``, which
# ``run_movement_dispatch`` catches and surfaces LOUD through
# ``movement.unresolved`` (the OTEL lie-detector) — never an uncaught raise,
# never a silent region_mode defer (which would hand a confabulated 'way up' to
# the narrator — the exact "convincing narration, zero mechanical backing" the
# OTEL principle exists to catch), never a bind to a null/phantom surface. These
# pin the same two findings against the new resolver: #1 (empty owner) and #3
# (unmapped owner).
# ---------------------------------------------------------------------------


def test_empty_site_owner_fails_loud_not_raises(capture_spans, deep_null_owner_kit):
    """Reviewer finding #1 (HIGH), site model: a ``frontier`` site whose
    ``attached_to`` is EMPTY must NOT crash the turn.

    ``resolve_exit_site`` raises ``SeamCrossingError(dangling_site_owner)`` on the
    empty owner; ``run_movement_dispatch`` wraps the resolver in ``try/except
    SeamCrossingError → _unresolved``, so the malformed site fails LOUD via
    ``movement.unresolved`` — never an uncaught raise, never a silent bind to a
    null surface, never a silent region defer.
    """
    kit = deep_null_owner_kit
    # The point of the guard: the SeamCrossingError must be caught — no raise.
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
    assert out.data.get("error") == "dangling_site_owner", (
        f"an empty site owner must fail loud with dangling_site_owner, got: {out.data}"
    )
    # PC never bound to a null/phantom surface — stays put on the entrance node.
    assert out.data.get("resolved_via") != "site_exit", (
        f"a malformed site owner must not complete an exit, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        "a malformed site owner must not move the PC off the entrance node; still at "
        f"{kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    # Fail LOUD: the GM panel must see the wiring fault — movement.unresolved, NOT
    # a silent region_mode defer, and no false site.exit / movement.resolved span
    # (resolve_exit_site raises BEFORE opening its span, so no PC was moved).
    span_names = [s.name for s in capture_spans.get_finished_spans()]
    unresolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.unresolved"]
    assert len(unresolved) == 1, (
        f"expected exactly one movement.unresolved span for the malformed site owner; "
        f"spans seen: {span_names}"
    )
    assert "movement.resolved" not in span_names, (
        "a malformed site owner must not emit a movement.resolved span"
    )
    assert "site.exit" not in span_names, (
        "a malformed site owner must not emit a site.exit span (it raised before the span)"
    )


def test_dangling_site_owner_does_not_bind_phantom_region(capture_spans, deep_dangling_owner_kit):
    """Reviewer finding #3 (MEDIUM), site model: the exit must not bind the PC to
    ``site.attached_to`` without verifying it names a real cartography region.

    ``resolve_exit_site`` guards ``attached_to in cartography.regions`` and raises
    ``SeamCrossingError(dangling_site_owner)`` otherwise. A site whose owner is a
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
    assert out.data.get("error") == "dangling_site_owner", (
        f"an unmapped site owner must fail loud with dangling_site_owner, got: {out.data}"
    )
    assert out.data.get("resolved_via") != "site_exit", (
        f"exit must not bind to an unmapped surface region, got: {out.data}"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        "PC bound to a phantom region "
        f"{kit.snapshot.region_for(perspective='Groucho')!r}; the resolved surface "
        "id must exist in cartography.regions"
    )
    span_names = [s.name for s in capture_spans.get_finished_spans()]
    unresolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.unresolved"]
    assert len(unresolved) == 1, (
        f"expected movement.unresolved for the dangling site owner; spans seen: {span_names}"
    )
    assert "site.exit" not in span_names, (
        "a dangling site owner must not emit a site.exit span (it raised before the span)"
    )
