"""Seam registry — recognition + fail-loud resolution (Story 105-2, spec §4 Piece 0)."""

import pytest

from sidequest.game.seams.base import SeamCrossingError, UnknownSeamKindError
from sidequest.game.seams.registry import (
    get_seam_resolver,
    seam_route_for,
    seam_route_via_adjacency,
)
from sidequest.genre.models.world import CartographyConfig, Region, Route


def _seam_cart() -> CartographyConfig:
    return CartographyConfig(
        starting_region="ropefoot",
        regions={
            "ropefoot": Region(
                name="Ropefoot",
                summary="The surface.",
                description="A camp.",
                adjacent=["the_dropmouth"],
            ),
            "the_dropmouth": Region(
                name="The Dropmouth",
                summary="The lip.",
                description="The mouth of the shaft.",
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
            Route(
                name="The Dead Road",
                description="The walk back out.",
                from_id="ropefoot",
                to_id="the_outside",  # NOT a registered seam kind
            ),
        ],
    )


def test_get_seam_resolver_unknown_kind_fails_loud():
    with pytest.raises(UnknownSeamKindError) as exc:
        get_seam_resolver("warp_gate")
    assert "warp_gate" in str(exc.value)
    assert "deep_descent" in str(exc.value)  # known kinds listed, ruleset-registry style


def test_get_seam_resolver_deep_descent_registered():
    assert callable(get_seam_resolver("deep_descent"))


def test_seam_route_for_finds_seam_route():
    route = seam_route_for(_seam_cart(), "the_dropmouth")
    assert route is not None
    assert route.name == "Down the Rope"
    assert route.to_id == "deep_descent"


def test_seam_route_for_ignores_plain_routes():
    # ropefoot's route exists but its to_id is not a registered seam kind.
    assert seam_route_for(_seam_cart(), "ropefoot") is None


def test_seam_route_for_region_without_routes():
    assert seam_route_for(_seam_cart(), "nonexistent_region") is None


def test_seam_route_for_none_cartography():
    assert seam_route_for(None, "the_dropmouth") is None


def test_seam_route_via_adjacency_finds_neighbor_seam():
    # ropefoot owns no seam, but is adjacent to the_dropmouth, which owns the
    # deep_descent seam — the one-step-from-the-camp descent (sq-playtest
    # 2026-06-21). The route returned is the_dropmouth's "Down the Rope".
    route = seam_route_via_adjacency(_seam_cart(), "ropefoot")
    assert route is not None
    assert route.name == "Down the Rope"
    assert route.to_id == "deep_descent"
    assert route.from_id == "the_dropmouth"


def test_seam_route_via_adjacency_none_when_neighbor_owns_no_seam():
    # the_dropmouth's only neighbor (ropefoot) owns no registered seam, so
    # there is no adjacent descent from the_dropmouth's perspective.
    assert seam_route_via_adjacency(_seam_cart(), "the_dropmouth") is None


def test_seam_route_via_adjacency_unknown_region():
    assert seam_route_via_adjacency(_seam_cart(), "nonexistent_region") is None


def test_seam_route_via_adjacency_none_cartography():
    assert seam_route_via_adjacency(None, "ropefoot") is None


def test_seam_route_via_adjacency_ambiguous_returns_none():
    # Two adjacent regions each own a seam → the caller must NOT guess which
    # descent was meant (No Silent Fallbacks). Mirrors surface_owner_for_entrance.
    cart = CartographyConfig(
        starting_region="hub",
        regions={
            "hub": Region(
                name="Hub",
                summary="Crossroads.",
                description="Two ways down.",
                adjacent=["pit_a", "pit_b"],
            ),
            "pit_a": Region(name="Pit A", summary="A.", description="A."),
            "pit_b": Region(name="Pit B", summary="B.", description="B."),
        },
        routes=[
            Route(name="Down A", description="d", from_id="pit_a", to_id="deep_descent"),
            Route(name="Down B", description="d", from_id="pit_b", to_id="deep_descent"),
        ],
    )
    assert seam_route_via_adjacency(cart, "hub") is None


def test_seam_crossing_error_carries_reason_and_surface():
    err = SeamCrossingError(reason="no_dungeon_entrance", surface="The descent has not formed.")
    assert err.reason == "no_dungeon_entrance"
    assert err.surface == "The descent has not formed."
