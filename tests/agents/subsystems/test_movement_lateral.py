"""Engine-authoritative lateral cartography travel (region-mode worlds).

Plan 1 of the location single-authority effort. The movement subsystem
resolves a lateral region-to-region move (oz: munchkin_country ->
the_emerald_city) against the cartography adjacency graph, instead of
deferring it to the narration title-scrape. Additive: unmatched intents
still defer.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems.movement import (
    _resolve_cartography_lateral,
    _tokens,
    run_movement_dispatch,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region, Route
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


def _oz_cartography_with_road() -> CartographyConfig:
    """oz-shaped region-mode world with TWO adjacent regions and no seam."""
    return CartographyConfig(
        starting_region="munchkin_country",
        navigation_mode=NavigationMode.region,
        regions={
            "munchkin_country": Region(
                name="Munchkin Country",
                summary="The land of the Munchkins.",
                description="A cheerful pastoral region.",
                adjacent=["the_emerald_city"],
            ),
            "the_emerald_city": Region(
                name="The Emerald City",
                summary="The green capital.",
                description="The Wizard's city.",
                adjacent=["munchkin_country"],
            ),
        },
        routes=[
            Route(
                name="Yellow Brick Road",
                description="The road to the Emerald City.",
                from_id="munchkin_country",
                to_id="the_emerald_city",  # NOT a registered seam kind
            ),
        ],
    )


def test_lateral_resolver_matches_named_neighbor():
    cart = _oz_cartography_with_road()
    target, via, ambiguous, candidates, surface = _resolve_cartography_lateral(
        cart=cart,
        from_region="munchkin_country",
        exit_descriptor="head to the Emerald City",
        direction="deeper",
        discovered_regions=["munchkin_country"],
    )
    assert target == "the_emerald_city"
    assert via == "region_lateral"
    assert ambiguous is False
    assert candidates == ["the_emerald_city"]


def test_lateral_resolver_no_overlap_is_no_match():
    cart = _oz_cartography_with_road()
    target, via, ambiguous, _candidates, _surface = _resolve_cartography_lateral(
        cart=cart,
        from_region="munchkin_country",
        exit_descriptor="I sit down and rest",
        direction="deeper",
        discovered_regions=["munchkin_country"],
    )
    assert target is None
    assert ambiguous is False


def test_lateral_resolver_back_uses_recency():
    cart = _oz_cartography_with_road()
    # PC is in the_emerald_city, came from munchkin_country (in discovered).
    target, via, ambiguous, _candidates, _surface = _resolve_cartography_lateral(
        cart=cart,
        from_region="the_emerald_city",
        exit_descriptor="",
        direction="back",
        discovered_regions=["munchkin_country", "the_emerald_city"],
    )
    assert target == "munchkin_country"
    assert via == "region_back"
    assert ambiguous is False


def test_lateral_resolver_back_picks_most_recent_of_multiple_neighbors():
    """back-recency sort must discriminate when more than one adjacent neighbor is discovered.

    With a single candidate the sort is vacuous — this fixture provides TWO discovered
    neighbors so the -recency key actually selects. arrival order: alpha first (index 0),
    beta second (index 1) → recency={alpha:0, beta:1, hub:2}; sort by -recency puts beta
    first, so beta wins.
    """
    cart = CartographyConfig(
        starting_region="hub",
        navigation_mode=NavigationMode.region,
        regions={
            "hub": Region(
                name="Hub",
                summary="",
                description="",
                adjacent=["alpha", "beta"],
            ),
            "alpha": Region(name="Alpha", summary="", description=""),
            "beta": Region(name="Beta", summary="", description=""),
        },
        routes=[],
    )
    target, via, ambiguous, _candidates, _surface = _resolve_cartography_lateral(
        cart=cart,
        from_region="hub",
        exit_descriptor="",
        direction="back",
        discovered_regions=["alpha", "beta", "hub"],  # beta is the most-recent prior neighbor
    )
    assert target == "beta"  # most-recently-arrived prior adjacent neighbor wins
    assert via == "region_back"
    assert ambiguous is False


def test_lateral_resolver_ambiguous_two_way_tie_fails_loud():
    cart = CartographyConfig(
        starting_region="crossroads",
        navigation_mode=NavigationMode.region,
        regions={
            "crossroads": Region(
                name="Crossroads",
                summary="A fork.",
                description="Two green roads diverge.",
                adjacent=["green_hill", "green_dale"],
            ),
            "green_hill": Region(name="Green Hill", summary="", description=""),
            "green_dale": Region(name="Green Dale", summary="", description=""),
        },
        routes=[],
    )
    target, _via, ambiguous, _candidates, surface = _resolve_cartography_lateral(
        cart=cart,
        from_region="crossroads",
        exit_descriptor="the green way",  # 'green' ties both neighbors
        direction="deeper",
        discovered_regions=["crossroads"],
    )
    assert target is None
    assert ambiguous is True
    assert "Which way?" in surface


def test_lateral_resolver_no_adjacency_is_no_match():
    cart = CartographyConfig(
        starting_region="island",
        navigation_mode=NavigationMode.region,
        regions={"island": Region(name="Island", summary="", description="")},
        routes=[],
    )
    target, _via, ambiguous, candidates, _surface = _resolve_cartography_lateral(
        cart=cart,
        from_region="island",
        exit_descriptor="anywhere",
        direction="deeper",
        discovered_regions=["island"],
    )
    assert target is None
    assert ambiguous is False
    assert candidates == []


# ---------------------------------------------------------------------------
# Dispatch-level tests (Task 2): engine crosses via apply_world_patch.
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _movement(direction: str, descriptor: str = "") -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"direction": direction, "exit_descriptor": descriptor},
        idempotency_key="mv-lateral",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(worlds={world_slug: world})


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-movement-lateral")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def test_dispatch_lateral_move_crosses_engine_side(capture_spans):
    cart = _oz_cartography_with_road()
    pack = _pack_with_cartography("oz", cart)
    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        pc_regions={"Dorothy": "munchkin_country"},
        player_seats={"p1": "Dorothy"},
    )
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "follow the road to the Emerald City"),
            snapshot=snap,
            player_name="Dorothy",
            dungeon_store=None,
            palette=None,
            pack=pack,
        )
    )
    assert out.data["resolved_via"] == "region_lateral", out.data
    assert out.data["to_region"] == "the_emerald_city"
    assert snap.region_for(perspective="Dorothy") == "the_emerald_city", (
        "engine must move the PC via apply_world_patch, not defer to narration"
    )
    resolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.resolved"]
    assert len(resolved) == 1
    attrs = resolved[0].attributes or {}
    assert attrs["resolved_via"] == "region_lateral"
    assert attrs["edge_kind"] == "cartography_adjacent"


def test_dispatch_unmatched_lateral_still_defers(capture_spans):
    cart = _oz_cartography_with_road()
    pack = _pack_with_cartography("oz", cart)
    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        pc_regions={"Dorothy": "munchkin_country"},
        player_seats={"p1": "Dorothy"},
    )
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "I look around the meadow"),
            snapshot=snap,
            player_name="Dorothy",
            dungeon_store=None,
            palette=None,
            pack=pack,
        )
    )
    assert out.data["resolved_via"] == "region_mode_deferred", out.data
    assert snap.region_for(perspective="Dorothy") == "munchkin_country", (
        "an unmatched intent must not move the PC (additive: defer, don't fail)"
    )


# ---------------------------------------------------------------------------
# Stopword scope pin: _tokens is article-clean; lateral resolver strips them.
# ---------------------------------------------------------------------------


def test_tokens_does_not_filter_stopwords():
    """The shared _tokens helper must NOT strip stopwords.

    Stopword filtering belongs ONLY in _resolve_cartography_lateral, not in
    the shared helper. Room-graph navigation (_resolve) and ordinal resolution
    (_resolve_ordinal) both use _tokens; stripping "down"/"back" would silently
    remove discriminating navigation tokens from exit descriptors.
    """
    result = _tokens("the emerald city")
    assert result == {"the", "emerald", "city"}, (
        "_tokens must include 'the' — stopwords are only stripped in the lateral resolver"
    )


def test_lateral_resolver_ignores_leading_article():
    """The lateral resolver must resolve through leading articles in display names.

    "to the Emerald City" should match region the_emerald_city (display name
    "The Emerald City") even though the shared _tokens now includes 'the'. The
    lateral resolver applies _STOPWORDS locally to both sides before scoring.
    """
    cart = _oz_cartography_with_road()
    target, via, ambiguous, _candidates, _surface = _resolve_cartography_lateral(
        cart=cart,
        from_region="munchkin_country",
        exit_descriptor="to the Emerald City",
        direction="deeper",
        discovered_regions=["munchkin_country"],
    )
    assert target == "the_emerald_city", (
        "lateral resolver must match 'to the Emerald City' → the_emerald_city "
        "after applying local stopword filter to both sides"
    )
    assert not ambiguous
