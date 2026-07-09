"""Track B, Task 5 (Story 164-3): enterable sites in the router state summary.

Companion to ``current_region_exits`` (Story 105-2, ``test_intent_router_region_exits.py``).
Task 5 teaches ``_build_state_summary`` to surface the sub-locations the acting PC
can ENTER from the current node — a tavern, a vault, the deep below a shaft — so
the intent router can classify "go into the tavern" / "down into the deep" as an
``enter_site`` movement instead of a vague adjacency step. Purely ADDITIVE: the
existing ``current_region_exits`` / seam projection is untouched.

``current_sites`` is built from ``SiteRegistry.sites_for_node(region_id)``, which
returns sites OWNED by the node PLUS sites owned by an ADJACENT node (the
one-action "down the rope at the camp" reach) — so the seam owner AND the
adjacent surface camp both surface the deep.

RED: ``_build_state_summary`` does not populate ``current_sites`` yet — Dev wires
it in GREEN (Task 5).
"""

from __future__ import annotations

import types

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
    SiteDecl,
)
from sidequest.server.intent_router_pass import _build_state_summary

# ---------------------------------------------------------------------------
# Fixtures — beneath_sunden-shaped, now declaring the deep as a frontier site.
# ---------------------------------------------------------------------------


def _frontier_site() -> SiteDecl:
    return SiteDecl(
        site_id="frontier",
        name="The Deep",
        archetype="megadungeon",
        attached_to="the_dropmouth",
        extent="frontier",
    )


def _cartography_with_site(*, sites: list[SiteDecl] | None = None) -> CartographyConfig:
    """Region-mode beneath_sunden shape: ``the_dropmouth`` owns the ``Down the
    Rope`` seam AND (when ``sites`` is given) the ``frontier`` site; ``ropefoot``
    is the adjacent surface camp."""
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
        sites=list(sites) if sites is not None else [],
    )


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    """Duck-typed GenrePack exposing only what ``_build_state_summary`` reads."""
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
        pc_regions={"Rux": region},
        player_seats={"p1": "Rux"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_state_summary_lists_enterable_site_from_owner() -> None:
    """The seam-owner node (``the_dropmouth``) surfaces its enterable site with
    the fields the router matches a descriptor against (site_id, name, archetype)."""
    pack = _pack_with_cartography(
        "beneath_sunden", _cartography_with_site(sites=[_frontier_site()])
    )
    summary = _build_state_summary(_snapshot("the_dropmouth"), pack=pack, acting_player="Rux")

    sites = summary.get("current_sites")
    assert sites is not None, "the seam-owner node must surface its enterable site"
    assert any(
        s["site_id"] == "frontier" and s["name"] == "The Deep" and s["archetype"] == "megadungeon"
        for s in sites
    ), sites


def test_state_summary_lists_enterable_site_via_adjacency() -> None:
    """``ropefoot`` owns no site, but it is one step from ``the_dropmouth`` which
    does — the one-action reach. ``sites_for_node`` includes adjacent-owned sites,
    so the camp surfaces the deep too (parity with the adjacent-seam projection)."""
    pack = _pack_with_cartography(
        "beneath_sunden", _cartography_with_site(sites=[_frontier_site()])
    )
    summary = _build_state_summary(_snapshot("ropefoot"), pack=pack, acting_player="Rux")

    sites = summary.get("current_sites", [])
    assert any(s["site_id"] == "frontier" for s in sites), (
        f"the adjacent camp must surface the deep as enterable, got: {sites}"
    )


def test_no_sites_declared_omits_current_sites() -> None:
    """Additive: a world that declares NO sites must not gain an empty
    ``current_sites`` key — the router's summary stays clean (no noise)."""
    pack = _pack_with_cartography("beneath_sunden", _cartography_with_site(sites=[]))
    summary = _build_state_summary(_snapshot("the_dropmouth"), pack=pack, acting_player="Rux")

    assert "current_sites" not in summary, (
        "no declared sites → the key must be absent, not an empty list"
    )


def test_node_with_no_enterable_site_omits_current_sites() -> None:
    """A node that neither owns nor is adjacent to a site-owning node surfaces no
    ``current_sites`` — the frontier site is attached to ``the_dropmouth`` and
    reachable from ``ropefoot``; a disconnected region sees nothing."""
    cart = CartographyConfig(
        starting_region="market_square",
        navigation_mode=NavigationMode.region,
        regions={
            "market_square": Region(name="Market Square", summary="Market.", description="Stalls."),
        },
        sites=[_frontier_site()],  # attached_to the_dropmouth, which is not on this map
    )
    pack = _pack_with_cartography("beneath_sunden", cart)
    summary = _build_state_summary(_snapshot("market_square"), pack=pack, acting_player="Rux")

    assert "current_sites" not in summary


def test_no_acting_player_omits_current_sites() -> None:
    """No acting PC → no per-PC region resolves → no ``current_sites`` projection,
    consistent with the ``current_region_exits`` per-PC contract (No Silent
    Fallbacks: never project against a party-consensus region)."""
    pack = _pack_with_cartography(
        "beneath_sunden", _cartography_with_site(sites=[_frontier_site()])
    )
    summary = _build_state_summary(_snapshot("the_dropmouth"), pack=pack)  # acting_player omitted

    assert "current_sites" not in summary
