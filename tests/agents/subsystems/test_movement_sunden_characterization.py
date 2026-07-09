"""Track B, Task 6 (Story 164-3): BEHAVIORAL-CONTRACT guard for the RISKY cutover.

Originally the Task 3 (Story 164-2) characterization guard that PINNED the CURRENT
Sünden movement/seam behavior before the ladder cutover. Story 164-3 performs that
cutover — ``movement.py``'s five-rung inlined seam ladder is replaced by
``SiteRegistry`` × the ``enter_site`` / ``exit_site`` resolvers — so this file is
now RETARGETED to the post-cutover contract, exactly as the original guard's
docstring anticipated:

    same DESTINATION (``to_region``), new internal path (``resolved_via`` migrates
    ``surface_descent`` / ``surface_descent_adjacent`` / ``surface_ascent`` →
    ``site_enter`` / ``site_exit``).

The seam crossings are now driven by the router's Task-5 vocabulary
(``action=enter_site`` / ``action=exit_site``), NOT the old ``direction`` +
``exit_descriptor`` seam signal. In-scene graph navigation (rung 4) still uses
``direction`` — the §Q1 navigator is UNCHANGED by the cutover — and the pure
region-mode lateral defer (rung 5) is unchanged too.

RED on ``develop``: the movement dispatch ignores ``action`` today, so an
``enter_site`` / ``exit_site`` dispatch resolves through the OLD ladder (or defers)
and returns the OLD ``resolved_via`` — the new-name assertions fail until Dev lands
the Task 6 cutover. The ``to_region`` invariance is what proves the rewrite is
behavior-preserving (the frontier keeps its legacy ``entrance`` node id; the
``graph.entrance_id`` fallback in ``resolve_enter_site`` is what preserves it).

Five rungs:

  1. owned-node enter    (the_dropmouth -> entrance)  resolved_via site_enter
  2. adjacent-node enter (ropefoot      -> entrance)  resolved_via site_enter
  3. site exit           (entrance -> the_dropmouth)  resolved_via site_exit
  4. in-dungeon navigation(entrance -> exp001.r0)     to_region exp001.r0, no error
  5. region-mode lateral  (unmatched descriptor)      resolved_via region_mode_deferred
"""

from __future__ import annotations

import asyncio
import types

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
# Doubles (mirror test_movement_party_split_158_7.py / test_movement_onward_ring.py)
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _enter(descriptor: str = "the deep") -> SubsystemDispatch:
    """The router's ``enter_site`` shape (Task 5)."""
    return SubsystemDispatch(
        subsystem="movement",
        params={"action": "enter_site", "site_descriptor": descriptor},
        idempotency_key="mv-164-3-char-enter",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


def _exit() -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"action": "exit_site"},
        idempotency_key="mv-164-3-char-exit",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


def _movement(direction: str, descriptor: str = "") -> SubsystemDispatch:
    """In-scene navigation shape (rungs 4-5): direction + free-text, no ``action``."""
    return SubsystemDispatch(
        subsystem="movement",
        params={"direction": direction, "exit_descriptor": descriptor},
        idempotency_key="mv-164-3-char-nav",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


class _FrontierLegacyStore:
    """DungeonStore/Repository double for Sünden's frontier site.

    The frontier keeps its LEGACY un-namespaced node ids for B1 (``entrance`` /
    ``expNNN.rN``): storage is ``(session, site_id)``-keyed, so node-id
    namespacing is a B4 follow-up. The loaded graph therefore anchors on
    ``ENTRANCE_ID`` (``entrance``), NOT the site's namespaced ``frontier:entrance``
    — modeling exactly the case ``resolve_enter_site``'s ``graph.entrance_id``
    fallback (164-3 carryover #1) preserves. Accepts BOTH the site-keyed resolver
    signature and the movement dispatch's own ``load_map(entrance_id=)`` call."""

    def load_map(self, *, entrance_id: str = ENTRANCE_ID, site_id: str = "frontier") -> RegionGraph:
        g = RegionGraph(entrance_id=ENTRANCE_ID)
        g.add_node(RegionNode(id=ENTRANCE_ID, expansion_id=0, theme="shaft_collar"))
        return g


class _StoreWithDeepGraph:
    """DungeonStore double: entrance + one materialized deep region below it (for
    the in-dungeon navigator, rung 4 — untouched by the cutover)."""

    def load_map(self, *, entrance_id: str, site_id: str = "frontier") -> RegionGraph:
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
    """Duck-typed ThemePalette — the seam crossing does not reach projection, but
    the signature requires a non-None palette."""

    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


def _hybrid_cartography() -> CartographyConfig:
    """beneath_sunden-shaped: region-mode declaring the deep as the ``frontier``
    site, attached to ``the_dropmouth``; ``ropefoot`` (the surface camp) is one
    step adjacent to it, so the site is reachable from either. The legacy
    ``deep_descent`` route is KEPT (inert once movement stops reading it — the
    plan retires it in a follow-up), modeling the real migrated cartography."""
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


def _lateral_cartography() -> CartographyConfig:
    """Two adjacent surface regions and NO seam routes or sites — a pure
    region-mode world. A move whose descriptor matches no adjacent region defers."""
    return CartographyConfig(
        starting_region="market_square",
        navigation_mode=NavigationMode.region,
        regions={
            "market_square": Region(
                name="Market Square",
                summary="The bustling market.",
                description="Stalls and crowds under awnings.",
                adjacent=["temple_row"],
            ),
            "temple_row": Region(
                name="Temple Row",
                summary="The temple district.",
                description="A quiet row of shrines.",
                adjacent=["market_square"],
            ),
        },
        routes=[],
    )


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    """Duck-typed GenrePack: exposes ``pack.worlds[slug].cartography``."""
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(worlds={world_slug: world})


def _snapshot(region: str, *, world_slug: str = "beneath_sunden") -> GameSnapshot:
    """Single-PC snapshot: PC ``Rux`` standing on ``region``."""
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug=world_slug,
        pc_regions={"Rux": region},
        player_seats={"p1": "Rux"},
    )


# ---------------------------------------------------------------------------
# Rung 1 — enter the site from the owning node.
# ---------------------------------------------------------------------------


def test_owned_node_enter_from_dropmouth() -> None:
    """PC on the site-owner region (``the_dropmouth``) entering crosses onto the
    frontier entrance via ``enter_site`` — same destination as the old owned-seam
    descent, new ``resolved_via``."""
    snap = _snapshot("the_dropmouth")
    out = _run(
        run_movement_dispatch(
            _enter("the deep"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FrontierLegacyStore(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _hybrid_cartography()),
        )
    )
    assert out.data.get("resolved_via") == "site_enter", out.data
    assert out.data.get("to_region") == ENTRANCE_ID, out.data
    assert snap.pc_regions["Rux"] == ENTRANCE_ID


# ---------------------------------------------------------------------------
# Rung 2 — enter the site from one step off the owner (adjacent reach).
# ---------------------------------------------------------------------------


def test_adjacent_node_enter_from_ropefoot() -> None:
    """PC on the surface camp (``ropefoot``, adjacent to the site owner) entering
    crosses onto the frontier entrance via ``enter_site`` — the one-action reach
    (``sites_for_node`` surfaces adjacent-owned sites). Same destination as the old
    adjacent-seam descent, new ``resolved_via``."""
    snap = _snapshot("ropefoot")
    out = _run(
        run_movement_dispatch(
            _enter("the deep"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FrontierLegacyStore(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _hybrid_cartography()),
        )
    )
    assert out.data.get("resolved_via") == "site_enter", out.data
    assert out.data.get("to_region") == ENTRANCE_ID, out.data
    assert snap.pc_regions["Rux"] == ENTRANCE_ID


# ---------------------------------------------------------------------------
# Rung 3 — exit the site back to the owning surface region.
# ---------------------------------------------------------------------------


def test_site_exit_returns_to_owning_region() -> None:
    """PC on the frontier entrance leaving via ``exit_site`` binds back to the
    surface region that OWNS the site (``the_dropmouth``, the site's ``attached_to``).
    The legacy ``entrance`` node is un-namespaced, so membership is detected via the
    ``is_procedural_region_id`` shim. Same destination as the old ascent, new
    ``resolved_via``."""
    snap = _snapshot(ENTRANCE_ID)
    out = _run(
        run_movement_dispatch(
            _exit(),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FrontierLegacyStore(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _hybrid_cartography()),
        )
    )
    assert out.data.get("resolved_via") == "site_exit", out.data
    assert out.data.get("to_region") == "the_dropmouth", out.data
    assert snap.pc_regions["Rux"] == "the_dropmouth"


# ---------------------------------------------------------------------------
# Rung 4 — in-dungeon graph navigation to a real neighbor (UNCHANGED by cutover).
# ---------------------------------------------------------------------------


def test_in_dungeon_navigation_steps_to_neighbor() -> None:
    """PC on a materialized dungeon node navigating deeper resolves to a real graph
    neighbor (``exp001.r0``) without error — the §Q1 navigator, NOT a seam crossing.
    The cutover leaves in-scene navigation on the ``direction`` vocabulary."""
    snap = _snapshot(ENTRANCE_ID)
    snap.discovered_regions.append(ENTRANCE_ID)
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "deeper into the dark"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_StoreWithDeepGraph(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _hybrid_cartography()),
        )
    )
    assert out.data.get("error") is None, out.data
    assert out.data.get("to_region") == "exp001.r0", out.data
    assert snap.pc_regions["Rux"] == "exp001.r0"


# ---------------------------------------------------------------------------
# Rung 5 — region-mode lateral defer (UNCHANGED by cutover).
# ---------------------------------------------------------------------------


def test_region_mode_unmatched_descriptor_defers() -> None:
    """A pure region-mode world (no sites) with an UNMATCHED travel descriptor
    defers cleanly (``region_mode_deferred``) — the PC does not move and the move is
    NOT an error (defer to narration, never fail-loud on a look-around)."""
    snap = _snapshot("market_square", world_slug="oz")
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "I look around the stalls"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=None,
            palette=None,
            pack=_pack_with_cartography("oz", _lateral_cartography()),
        )
    )
    assert out.data.get("resolved_via") == "region_mode_deferred", out.data
    assert snap.pc_regions["Rux"] == "market_square", "an unmatched intent must not move the PC"
