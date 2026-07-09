"""Per-connection scene context (world | site:<id>) — Track B, Task 7 (story 164-4).

``resolve_scene_context`` decides which map projection owns THIS
connection's turn: ``("world", None)`` when the PC stands on the authored
cartography, ``("site", site_id)`` when the PC's region is a node inside a
site's graph. It replaces ``map_emit._descent_phase``'s
beneath_sunden-hardcoded ``surface|deep`` binary so ANY site — a bounded
tavern/vault with ``{site_id}:``-namespaced nodes, or the legacy Sünden
frontier with bare ``entrance``/``expNNN.rN`` ids — can project its
interior map.

Resolution contract under test:
  - owner-namespaced node (``gilded_boar:r2``) -> its site, via
    ``SiteRegistry.site_owning_node`` — pure, NO store probe;
  - bare node + a declared ``extent: frontier`` site -> membership check
    against that site's store (the Sünden legacy path; dies with the B4
    namespacing follow-up);
  - everything else (cartography region, unseated connection, missing
    ``pc_regions`` entry, no repo) -> world scene, never a crash.

CONTENT-FREE: synthetic ``CartographyConfig`` + hand-built snapshots; the
frontier store is a duck-typed double, never a live pack
(``feedback_no_content_coupled_tests``).
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest
from sidequest.server.scene_context import SceneContext, resolve_scene_context

from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    SiteDecl,
)

FRONTIER_DECL = SiteDecl(
    site_id="frontier",
    name="The Deep",
    archetype="megadungeon",
    attached_to="the_dropmouth",
    extent="frontier",
)

TAVERN_DECL = SiteDecl(
    site_id="gilded_boar",
    name="The Gilded Boar",
    archetype="tavern",
    attached_to="village_green",
    extent="bounded",
)


def _region(name: str, adjacent: list[str]) -> Region:
    return Region(name=name, summary="x", description="x", adjacent=adjacent)


class _FrontierStore:
    """Duck-typed dungeon-repository double: ``load_map(...)`` returns a
    graph-like object with ``.nodes``. Signature-agnostic (``**kwargs``) so
    the membership probe's exact call shape stays an implementation
    detail — mirrors the 164-3 lesson that a too-faithful-to-the-test-double
    (rather than to the repo) shape let a real break ship green."""

    def __init__(self, node_ids: tuple[str, ...]) -> None:
        self._nodes = {nid: object() for nid in node_ids}
        self.calls: list[dict[str, Any]] = []

    def load_map(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(nodes=dict(self._nodes))


class _PoisonStore:
    """Fails the test if the store is probed at all."""

    def load_map(self, **kwargs: Any) -> Any:  # pragma: no cover - failure path
        raise AssertionError(
            "the site store must NOT be probed when no frontier site can own the region"
        )


def _sd(
    *,
    world_slug: str = "beneath_sunden",
    genre_slug: str = "caverns_and_claudes",
    regions: dict[str, Region] | None = None,
    sites: list[SiteDecl] | None = None,
    repo: Any = None,
    with_repo_attr: bool = True,
) -> Any:
    if regions is None:
        regions = {
            "ropefoot": _region("Ropefoot", ["the_dropmouth"]),
            "the_dropmouth": _region("The Dropmouth", ["ropefoot"]),
        }
    cart = CartographyConfig(
        navigation_mode=NavigationMode.region,
        regions=regions,
        sites=sites or [],
    )
    world_obj = SimpleNamespace(cartography=cart)
    pack = SimpleNamespace(worlds={world_slug: world_obj})
    kwargs: dict[str, Any] = {
        "genre_pack": pack,
        "world_slug": world_slug,
        "genre_slug": genre_slug,
        "player_id": "p1",
    }
    if with_repo_attr:
        kwargs["dungeon_repository"] = repo
    return SimpleNamespace(**kwargs)


def _tavern_sd(*, repo: Any = None, with_repo_attr: bool = True) -> Any:
    return _sd(
        world_slug="kettleford",
        genre_slug="tea_time",
        regions={
            "village_green": _region("Village Green", ["high_street"]),
            "high_street": _region("High Street", ["village_green"]),
        },
        sites=[TAVERN_DECL],
        repo=repo,
        with_repo_attr=with_repo_attr,
    )


def _snapshot(
    *,
    pc_region: str | None,
    world_slug: str = "beneath_sunden",
    genre_slug: str = "caverns_and_claudes",
    seat_pc: bool = True,
) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug=genre_slug,
        world_slug=world_slug,
        turn_manager=TurnManager(),
    )
    if seat_pc:
        snap.player_seats = {"p1": "Rux"}
        if pc_region is not None:
            snap.pc_regions = {"Rux": pc_region}
            snap.current_region = pc_region
    return snap


# --------------------------------------------------------------------------
# world scene: PC on the authored cartography
# --------------------------------------------------------------------------
def test_world_scene_when_pc_on_cartography() -> None:
    sd = _sd(sites=[FRONTIER_DECL], repo=_FrontierStore(("entrance", "exp001.r2")))
    snap = _snapshot(pc_region="ropefoot")

    ctx = resolve_scene_context(sd=sd, snapshot=snap, player_id="p1")

    assert ctx == SceneContext(kind="world", site_id=None)


# --------------------------------------------------------------------------
# site scene: legacy Sünden frontier (bare node ids, store membership)
# --------------------------------------------------------------------------
def test_site_scene_when_pc_in_legacy_frontier_deep() -> None:
    sd = _sd(sites=[FRONTIER_DECL], repo=_FrontierStore(("entrance", "exp001.r2")))
    snap = _snapshot(pc_region="exp001.r2")

    ctx = resolve_scene_context(sd=sd, snapshot=snap, player_id="p1")

    assert ctx.kind == "site"
    assert ctx.site_id == "frontier"


def test_site_scene_at_legacy_frontier_entrance() -> None:
    """The bootstrap anchor node itself — the exact seam 164-3's CRITICAL
    finding lived on (``entrance`` is bare, never namespaced, in B1)."""
    sd = _sd(sites=[FRONTIER_DECL], repo=_FrontierStore(("entrance", "exp001.r2")))
    snap = _snapshot(pc_region="entrance")

    ctx = resolve_scene_context(sd=sd, snapshot=snap, player_id="p1")

    assert ctx == SceneContext(kind="site", site_id="frontier")


# --------------------------------------------------------------------------
# site scene: owner-namespaced node (bounded site) — pure, no store probe
# --------------------------------------------------------------------------
def test_site_scene_for_namespaced_bounded_site_node() -> None:
    """A bounded site's nodes are ``{site_id}:``-namespaced; ownership
    resolves through the registry alone. ``repo=None`` proves the store is
    not needed on this path."""
    sd = _tavern_sd(repo=None)
    snap = _snapshot(pc_region="gilded_boar:r2", world_slug="kettleford", genre_slug="tea_time")

    ctx = resolve_scene_context(sd=sd, snapshot=snap, player_id="p1")

    assert ctx == SceneContext(kind="site", site_id="gilded_boar")


def test_bounded_sites_never_probe_the_store() -> None:
    """No frontier site is declared, so the legacy membership fallback has
    nothing to check — a PC on plain cartography must resolve to the world
    scene WITHOUT touching the store (the poison double raises if probed)."""
    sd = _tavern_sd(repo=_PoisonStore())
    snap = _snapshot(pc_region="village_green", world_slug="kettleford", genre_slug="tea_time")

    ctx = resolve_scene_context(sd=sd, snapshot=snap, player_id="p1")

    assert ctx == SceneContext(kind="world", site_id=None)


# --------------------------------------------------------------------------
# world scene: degenerate connections (spectator / regionless / no repo)
# --------------------------------------------------------------------------
def test_world_scene_when_no_seated_pc() -> None:
    sd = _sd(sites=[FRONTIER_DECL], repo=_FrontierStore(("entrance",)))
    snap = _snapshot(pc_region="exp001.r2")

    ctx = resolve_scene_context(sd=sd, snapshot=snap, player_id="ghost-spectator")

    assert ctx == SceneContext(kind="world", site_id=None)


def test_world_scene_when_seated_pc_has_no_region() -> None:
    sd = _sd(sites=[FRONTIER_DECL], repo=_FrontierStore(("entrance",)))
    snap = _snapshot(pc_region=None)  # seated, but no pc_regions entry

    ctx = resolve_scene_context(sd=sd, snapshot=snap, player_id="p1")

    assert ctx == SceneContext(kind="world", site_id=None)


def test_world_scene_when_frontier_store_lacks_region() -> None:
    """A bare region that is in NO site's store is a cartography region the
    engine simply hasn't authored — world scene, not a phantom site."""
    sd = _sd(sites=[FRONTIER_DECL], repo=_FrontierStore(("entrance",)))
    snap = _snapshot(pc_region="exp042.r9")

    ctx = resolve_scene_context(sd=sd, snapshot=snap, player_id="p1")

    assert ctx == SceneContext(kind="world", site_id=None)


def test_world_scene_without_dungeon_repository_attr() -> None:
    """A session shape with no ``dungeon_repository`` at all (non-dungeon
    stacks) must degrade to the world scene, never AttributeError."""
    sd = _sd(sites=[FRONTIER_DECL], with_repo_attr=False)
    snap = _snapshot(pc_region="exp001.r2")

    ctx = resolve_scene_context(sd=sd, snapshot=snap, player_id="p1")

    assert ctx == SceneContext(kind="world", site_id=None)


# --------------------------------------------------------------------------
# type invariant: the context is a frozen value object
# --------------------------------------------------------------------------
def test_scene_context_is_frozen() -> None:
    ctx = SceneContext(kind="world", site_id=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.kind = "site"  # type: ignore[misc]
