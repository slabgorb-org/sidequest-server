"""Per-connection scene context (world | site:<id>) — Track B, Task 7.

RED (story 164-4): ``sidequest.server.scene_context`` does not exist yet —
this whole file fails with ModuleNotFoundError until the Task 7 cutover lands.

Contract under test (plan 2026-07-08-mapping-track-b-site-system.md §Task 7,
consumed by Tasks 8/9): ``resolve_scene_context(*, sd, snapshot, player_id)
-> SceneContext`` where ``SceneContext`` is frozen ``(kind="world",
site_id=None)`` or ``(kind="site", site_id=<owning site>)``. A connection is
in a site scene iff its PC's region is a node in some site's graph:

  - owner-namespaced nodes (``gilded_boar:r2``) resolve via the SiteRegistry
    namespace alone — no store IO;
  - the legacy un-namespaced frontier nodes (``entrance``/``expNNN.rN``)
    resolve via frontier-site store membership;
  - everything else — cartography regions, unseated connections, worlds with
    no ``sites:`` — is the world scene.

CONTENT-FREE (feedback_no_content_coupled_tests): synthetic worlds/graphs and
a duck-typed repository stub — never a live pack.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    SiteDecl,
)
from sidequest.server.scene_context import SceneContext, resolve_scene_context

# ---------------------------------------------------------------------------
# Content-free doubles (same shape as tests/server/test_descent_phase_map_switch.py)
# ---------------------------------------------------------------------------


def _region(name: str, adjacent: list[str]) -> Region:
    return Region(name=name, summary=f"{name}.", description=f"{name}.", adjacent=adjacent)


def _cartography(
    regions: dict[str, Region], sites: list[SiteDecl] | None = None
) -> CartographyConfig:
    return CartographyConfig(
        navigation_mode=NavigationMode.region,
        regions=regions,
        sites=sites or [],
    )


def _sd(
    *,
    world_slug: str,
    genre_slug: str,
    cartography: CartographyConfig,
    repo: Any = None,
) -> Any:
    world_obj = SimpleNamespace(cartography=cartography)
    pack = SimpleNamespace(worlds={world_slug: world_obj})
    return SimpleNamespace(
        genre_pack=pack,
        world_slug=world_slug,
        genre_slug=genre_slug,
        player_id="p1",
        dungeon_repository=repo,
    )


def _snapshot(
    *,
    genre_slug: str,
    world_slug: str,
    pc_region: str | None,
    seated: bool = True,
) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug=genre_slug,
        world_slug=world_slug,
        turn_manager=TurnManager(),
    )
    if seated:
        snap.player_seats = {"p1": "Tex"}
        if pc_region is not None:
            snap.pc_regions = {"Tex": pc_region}
            snap.current_region = pc_region
    return snap


def _graph(entrance: str, extra: tuple[str, ...] = ()) -> RegionGraph:
    g = RegionGraph(entrance_id=entrance)
    g.add_node(RegionNode(id=entrance, expansion_id=0, theme="t"))
    for i, rid in enumerate(extra, start=1):
        g.add_node(RegionNode(id=rid, expansion_id=i, theme="t"))
    return g


class _StubDungeonRepo:
    """Duck-typed DungeonRepository: the resolver consumes only
    ``load_map(*, entrance_id, site_id)``. Records calls for diagnostics;
    the tests pin OUTCOMES, never argument plumbing (the plan's embedded
    snippet is known-buggy — hand-verified behavior is the contract)."""

    def __init__(self, graphs_by_site: dict[str, RegionGraph]) -> None:
        self._graphs = graphs_by_site
        self.calls: list[tuple[str, str]] = []

    def load_map(self, *, entrance_id: str, site_id: str = "frontier") -> RegionGraph:
        self.calls.append((entrance_id, site_id))
        g = self._graphs.get(site_id)
        return g if g is not None else RegionGraph(entrance_id=entrance_id)


# A non-sunden world with one bounded, namespaced site (the fence-dissolution
# shape: NOT caverns_and_claudes/beneath_sunden).
_TAVERN_SITE = SiteDecl(
    site_id="gilded_boar",
    name="The Gilded Boar",
    archetype="tavern",
    attached_to="dustcross",
    extent="bounded",
)


def _tavern_world_sd(repo: Any = None) -> Any:
    return _sd(
        world_slug="gilded_reach",
        genre_slug="spaghetti_western",
        cartography=_cartography({"dustcross": _region("Dustcross", [])}, sites=[_TAVERN_SITE]),
        repo=repo,
    )


# The sunden shape post-164-3: cartography surface + a declared frontier site
# whose graph nodes are the LEGACY un-namespaced ids (entrance/expNNN.rN).
_FRONTIER_SITE = SiteDecl(
    site_id="frontier",
    name="The Deep",
    archetype="megadungeon",
    attached_to="the_dropmouth",
    extent="frontier",
)


def _sunden_sd(repo: Any) -> Any:
    return _sd(
        world_slug="beneath_sunden",
        genre_slug="caverns_and_claudes",
        cartography=_cartography(
            {
                "ropefoot": _region("Ropefoot", ["the_dropmouth"]),
                "the_dropmouth": _region("The Dropmouth", ["ropefoot"]),
            },
            sites=[_FRONTIER_SITE],
        ),
        repo=repo,
    )


def _frontier_repo() -> _StubDungeonRepo:
    return _StubDungeonRepo({"frontier": _graph("entrance", extra=("exp001.r2",))})


# ---------------------------------------------------------------------------
# world scene
# ---------------------------------------------------------------------------


def test_world_scene_when_pc_on_cartography_region() -> None:
    """PC above the rope on a declared-site world: world scene."""
    ctx = resolve_scene_context(
        sd=_sunden_sd(_frontier_repo()),
        snapshot=_snapshot(
            genre_slug="caverns_and_claudes",
            world_slug="beneath_sunden",
            pc_region="ropefoot",
        ),
        player_id="p1",
    )
    assert ctx == SceneContext(kind="world", site_id=None)


def test_world_scene_when_connection_has_no_seated_pc() -> None:
    """Spectator / GM-panel connection (no seat): world scene, never a crash."""
    ctx = resolve_scene_context(
        sd=_tavern_world_sd(),
        snapshot=_snapshot(
            genre_slug="spaghetti_western",
            world_slug="gilded_reach",
            pc_region=None,
            seated=False,
        ),
        player_id="p1",
    )
    assert ctx == SceneContext(kind="world", site_id=None)


def test_world_scene_when_world_declares_no_sites() -> None:
    """A site-less region world (the common case) is always the world scene —
    the inert SiteRegistry answers every query with 'no site' and the
    resolver must not require a dungeon_repository."""
    sd = _sd(
        world_slug="burning_peace",
        genre_slug="elemental_harmony",
        cartography=_cartography({"edo": _region("Edo", [])}),
        repo=None,
    )
    ctx = resolve_scene_context(
        sd=sd,
        snapshot=_snapshot(
            genre_slug="elemental_harmony",
            world_slug="burning_peace",
            pc_region="edo",
        ),
        player_id="p1",
    )
    assert ctx == SceneContext(kind="world", site_id=None)


def test_world_scene_for_unregistered_namespace() -> None:
    """A namespaced node whose prefix matches NO declared site is not a site
    scene — an unknown namespace must not fabricate a site_id."""
    ctx = resolve_scene_context(
        sd=_tavern_world_sd(),
        snapshot=_snapshot(
            genre_slug="spaghetti_western",
            world_slug="gilded_reach",
            pc_region="ghost_site:r1",
        ),
        player_id="p1",
    )
    assert ctx == SceneContext(kind="world", site_id=None)


# ---------------------------------------------------------------------------
# site scene
# ---------------------------------------------------------------------------


def test_site_scene_for_namespaced_node_needs_no_store() -> None:
    """Owner-namespaced node (gilded_boar:r2): resolves via the registry
    namespace ALONE — dungeon_repository is None and must not be touched."""
    ctx = resolve_scene_context(
        sd=_tavern_world_sd(repo=None),
        snapshot=_snapshot(
            genre_slug="spaghetti_western",
            world_slug="gilded_reach",
            pc_region="gilded_boar:r2",
        ),
        player_id="p1",
    )
    assert ctx.kind == "site"
    assert ctx.site_id == "gilded_boar"


def test_site_scene_for_legacy_frontier_node_via_store_membership() -> None:
    """The legacy un-namespaced deep (exp001.r2): resolves as the frontier
    site because the node is in the frontier site's stored graph."""
    ctx = resolve_scene_context(
        sd=_sunden_sd(_frontier_repo()),
        snapshot=_snapshot(
            genre_slug="caverns_and_claudes",
            world_slug="beneath_sunden",
            pc_region="exp001.r2",
        ),
        player_id="p1",
    )
    assert ctx.kind == "site"
    assert ctx.site_id == "frontier"


def test_legacy_entrance_node_is_the_frontier_site_scene() -> None:
    """The un-namespaced ``entrance`` anchor itself is inside the site."""
    ctx = resolve_scene_context(
        sd=_sunden_sd(_frontier_repo()),
        snapshot=_snapshot(
            genre_slug="caverns_and_claudes",
            world_slug="beneath_sunden",
            pc_region="entrance",
        ),
        player_id="p1",
    )
    assert ctx.kind == "site"
    assert ctx.site_id == "frontier"


def test_world_scene_when_seated_pc_has_no_region() -> None:
    """Seated PC with no ``pc_regions`` entry: world scene, never a crash.
    (Coverage test, passes on GREEN — ported from the abandoned 2026-07-09
    164-4 branch ``d57a24ce`` so its extra pin isn't lost.)"""
    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        world_slug="gilded_reach",
        turn_manager=TurnManager(),
    )
    snap.player_seats = {"p1": "Tex"}  # seated, but pc_regions stays empty
    ctx = resolve_scene_context(sd=_tavern_world_sd(), snapshot=snap, player_id="p1")
    assert ctx == SceneContext(kind="world", site_id=None)


def test_world_scene_without_dungeon_repository_attr() -> None:
    """An sd that lacks the ``dungeon_repository`` attribute ENTIRELY (not
    just None) must resolve the legacy-frontier branch to the world scene —
    the 165-3 dead-attribute trap shape: ``getattr(sd, ..., None)`` on a
    real object, no store probe, no crash. (Coverage test, passes on GREEN —
    ported from the abandoned 2026-07-09 164-4 branch ``d57a24ce``.)"""
    sd = SimpleNamespace(
        genre_pack=_sunden_sd(None).genre_pack,
        world_slug="beneath_sunden",
        genre_slug="caverns_and_claudes",
        player_id="p1",
        # deliberately NO dungeon_repository field
    )
    ctx = resolve_scene_context(
        sd=sd,
        snapshot=_snapshot(
            genre_slug="caverns_and_claudes",
            world_slug="beneath_sunden",
            pc_region="exp001.r2",
        ),
        player_id="p1",
    )
    assert ctx == SceneContext(kind="world", site_id=None)


# ---------------------------------------------------------------------------
# model invariants
# ---------------------------------------------------------------------------


def test_scene_context_is_frozen() -> None:
    """SceneContext is a read-only snapshot handed across layers — mutation
    would desync map arbitration mid-turn (same invariant as SiteDescriptor)."""
    ctx = SceneContext(kind="world", site_id=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, ctx).kind = "site"
