"""Track B, Task 4 (Story 164-2): symmetric enter_site / exit_site seam resolvers.

The two resolvers are the site-parameterized replacements for the asymmetric
``deep_descent`` / directly-called ``surface_ascent`` pair. ``enter_site`` binds
THIS PC onto a site's entrance node; ``exit_site`` binds it back to the site's
owning cartography region. Both fail LOUD (recoverable ``SeamCrossingError``)
rather than stranding the PC in a phantom node.

Additive for B1 — nothing on the movement hot path calls these yet (Task 6 wires
them). This suite proves they exist, bind ``pc_region`` correctly, fail loud on
the two wiring faults, and are REACHABLE through the seam registry.

RED: ``sidequest.game.sites.enter_site`` / ``exit_site`` do not exist yet — Dev
creates them in GREEN.
"""

from __future__ import annotations

import pytest

from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
from sidequest.game.seams.base import SeamCrossingError
from sidequest.game.session import GameSnapshot
from sidequest.game.sites import SiteDescriptor
from sidequest.game.sites.enter_site import resolve_enter_site
from sidequest.game.sites.exit_site import resolve_exit_site
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
)

_FRONTIER = SiteDescriptor(
    site_id="frontier",
    name="The Deep",
    archetype="megadungeon",
    attached_to="the_dropmouth",
    extent="frontier",
)


class _SiteStore:
    """DungeonRepository double: ``load_map(entrance_id=, site_id=)`` returns a
    per-site graph. ``entrance_present`` controls whether the entrance node has
    been materialized (the wiring-fault case leaves it absent)."""

    def __init__(self, *, entrance_present: bool = True) -> None:
        self._entrance_present = entrance_present
        self.calls: list[tuple[str, str]] = []

    def load_map(self, *, entrance_id: str, site_id: str = "frontier") -> RegionGraph:
        self.calls.append((entrance_id, site_id))
        g = RegionGraph(entrance_id=entrance_id)
        if self._entrance_present:
            g.add_node(RegionNode(id=entrance_id, expansion_id=0, theme="hewn_stone"))
        return g


class _LegacyEntranceStore:
    """Story 164-3 frontier-legacy case: the store's graph entrance is the
    un-namespaced legacy ``entrance`` (ENTRANCE_ID), NOT the site's namespaced
    ``entrance_node_id`` (``frontier:entrance``). Sünden's frontier keeps its
    legacy node ids for B1 (storage is site-keyed; node-id namespacing is a B4
    follow-up), so the graph the store returns anchors on ``entrance``. The
    resolver must fall back to the real legacy ``ENTRANCE_ID`` node when the
    declared namespaced node is absent — a LOUD, single fallback, harmless for
    bounded sites."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def load_map(self, *, entrance_id: str, site_id: str = "frontier") -> RegionGraph:
        self.calls.append((entrance_id, site_id))
        # Mirror the REAL PgDungeonRepository.load_map (game/pg/dungeon.py):
        # ``graph.entrance_id`` echoes the entrance_id the CALLER passed in — it is
        # NOT independently "the graph's own entrance". The frontier-legacy NODES,
        # though, keep the un-namespaced legacy ``entrance`` id (B1; namespacing is
        # a B4 follow-up). The earlier double hard-coded ``entrance_id="entrance"``,
        # which silently diverged from production and hid the dead-fallback bug.
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(RegionNode(id="entrance", expansion_id=0, theme="shaft_collar"))
        return g


def _snapshot(region: str) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Rux": region},
        player_seats={"p1": "Rux"},
    )


def _cartography() -> CartographyConfig:
    return CartographyConfig(
        starting_region="the_dropmouth",
        navigation_mode=NavigationMode.region,
        regions={
            "the_dropmouth": Region(
                name="The Dropmouth", summary="The lip.", description="The shaft mouth."
            ),
        },
    )


# ---------------------------------------------------------------------------
# enter_site — binds the PC onto the site entrance, keyed by site_id.
# ---------------------------------------------------------------------------


def test_enter_site_binds_pc_to_site_entrance() -> None:
    """A resolved enter binds THIS PC onto ``site.entrance_node_id`` and loads the
    graph keyed by the site (proving per-site storage isolation is threaded)."""
    snap = _snapshot("the_dropmouth")
    store = _SiteStore(entrance_present=True)

    result = resolve_enter_site(
        snapshot=snap,
        player_name="Rux",
        site=_FRONTIER,
        dungeon_repository=store,
        resolved_via="site_enter",
    )

    assert result.to_region == "frontier:entrance"
    assert snap.region_for(perspective="Rux") == "frontier:entrance"
    assert snap.pc_regions["Rux"] == "frontier:entrance"
    # The store was queried under the site's own key, not a global one.
    assert store.calls == [("frontier:entrance", "frontier")]


def test_enter_site_missing_store_raises_recoverable() -> None:
    """No site store == a wiring fault, not a closed door: fail loud with a
    recoverable ``SeamCrossingError(reason=no_site_store)`` and DO NOT move the PC."""
    snap = _snapshot("the_dropmouth")

    with pytest.raises(SeamCrossingError) as ei:
        resolve_enter_site(
            snapshot=snap,
            player_name="Rux",
            site=_FRONTIER,
            dungeon_repository=None,
            resolved_via="site_enter",
        )

    assert ei.value.reason == "no_site_store"
    assert snap.pc_regions["Rux"] == "the_dropmouth", "a failed enter must not move the PC"


def test_enter_site_frontier_legacy_binds_seed_entrance() -> None:
    """Story 164-3 (carryover #1) / 164-8 fix: the Sünden frontier site's graph uses
    the legacy un-namespaced ``entrance`` node, not the site's namespaced
    ``frontier:entrance``. When the declared ``entrance_node_id`` is absent from the
    graph, the resolver binds the PC to the real legacy ``ENTRANCE_ID`` node (a loud
    single fallback) — it does NOT raise ``no_site_entrance``.

    Regression guard for 164-8: the fallback must key off ``ENTRANCE_ID``, NOT
    ``graph.entrance_id`` (which ``load_map`` sets to the caller-passed id, == the
    absent namespaced target — dead code). The prior double hid this by hard-coding
    ``graph.entrance_id="entrance"``; ``_LegacyEntranceStore`` now mirrors the real
    repository, so this test fails if the dead-fallback regression returns."""
    snap = _snapshot("the_dropmouth")
    store = _LegacyEntranceStore()

    result = resolve_enter_site(
        snapshot=snap,
        player_name="Rux",
        site=_FRONTIER,
        dungeon_repository=store,
        resolved_via="site_enter",
    )

    # Bound to the graph's actual (legacy) entrance, not the namespaced declared id.
    assert result.to_region == "entrance"
    assert snap.region_for(perspective="Rux") == "entrance"
    assert snap.pc_regions["Rux"] == "entrance"


def test_enter_site_missing_entrance_node_raises() -> None:
    """The store exists but NEITHER the declared entrance node NOR a usable
    ``graph.entrance_id`` was materialized — fail loud (``reason=no_site_entrance``)
    rather than binding the PC to a phantom node.

    164-3 note: this guards the frontier-legacy fallback
    (``test_enter_site_frontier_legacy_prefers_graph_entrance``) against inventing
    a phantom entrance — the fallback may only bind to a ``graph.entrance_id`` that
    is a REAL node. Here the graph is empty (its ``entrance_id`` is not a node), so
    the fallback cannot apply and the resolver must still raise."""
    snap = _snapshot("the_dropmouth")
    store = _SiteStore(entrance_present=False)

    with pytest.raises(SeamCrossingError) as ei:
        resolve_enter_site(
            snapshot=snap,
            player_name="Rux",
            site=_FRONTIER,
            dungeon_repository=store,
            resolved_via="site_enter",
        )

    assert ei.value.reason == "no_site_entrance"
    assert snap.pc_regions["Rux"] == "the_dropmouth"


# ---------------------------------------------------------------------------
# exit_site — binds the PC back to the site's owning cartography region.
# ---------------------------------------------------------------------------


def test_exit_site_binds_pc_to_attached_region() -> None:
    """A resolved exit binds THIS PC back to ``site.attached_to`` (the owning
    cartography region)."""
    snap = _snapshot("frontier:entrance")

    result = resolve_exit_site(
        snapshot=snap,
        player_name="Rux",
        site=_FRONTIER,
        cartography=_cartography(),
        resolved_via="site_exit",
    )

    assert result.to_region == "the_dropmouth"
    assert snap.region_for(perspective="Rux") == "the_dropmouth"
    assert snap.pc_regions["Rux"] == "the_dropmouth"


def test_exit_site_dangling_owner_raises() -> None:
    """The site's ``attached_to`` region is not on the map — fail loud
    (``reason=dangling_site_owner``) rather than stranding the PC on a phantom
    surface region."""
    orphan = SiteDescriptor(
        site_id="frontier",
        name="The Deep",
        archetype="megadungeon",
        attached_to="nowhere",
        extent="frontier",
    )
    snap = _snapshot("frontier:entrance")

    with pytest.raises(SeamCrossingError) as ei:
        resolve_exit_site(
            snapshot=snap,
            player_name="Rux",
            site=orphan,
            cartography=_cartography(),
            resolved_via="site_exit",
        )

    assert ei.value.reason == "dangling_site_owner"
    assert snap.pc_regions["Rux"] == "frontier:entrance"


def test_exit_site_missing_cartography_raises_distinct_reason() -> None:
    """Cartography was never threaded (a WIRING fault) — fail loud with its OWN
    reason (``no_cartography``), NOT ``dangling_site_owner`` (a DATA fault). The
    GM panel / fail-loud doctrine reads ``SeamCrossingError.reason`` to route
    debugging, so a wiring fault must not masquerade as a bad-owner data fault.
    Mirrors enter_site's ``no_site_store`` vs ``no_site_entrance`` split."""
    snap = _snapshot("frontier:entrance")

    with pytest.raises(SeamCrossingError) as ei:
        resolve_exit_site(
            snapshot=snap,
            player_name="Rux",
            site=_FRONTIER,
            cartography=None,
            resolved_via="site_exit",
        )

    assert ei.value.reason == "no_cartography"
    assert snap.pc_regions["Rux"] == "frontier:entrance"


# ---------------------------------------------------------------------------
# Wiring (SM-required): the resolvers are REACHABLE through the seam registry,
# not merely importable — get_seam_resolver returns the exact resolver objects.
# ---------------------------------------------------------------------------


def test_enter_exit_site_registered_in_seam_registry() -> None:
    """``enter_site`` / ``exit_site`` are registered seam kinds — Task 6 dispatches
    them by kind, so the registry lookup must return the resolver functions."""
    from sidequest.game.seams.registry import get_seam_resolver

    assert get_seam_resolver("enter_site") is resolve_enter_site
    assert get_seam_resolver("exit_site") is resolve_exit_site
