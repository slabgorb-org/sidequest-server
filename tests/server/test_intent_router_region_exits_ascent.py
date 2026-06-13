"""current_region_exits at the dungeon entrance node names the surface exit
(Story 105-3 AC3, the reverse of test_intent_router_region_exits.py).

105-2 surfaced a surface region's onward exits (adjacency + seam routes) into the
state summary the intent router sees, so a descent is classified as movement. The
return trip needs the same lexical bridge from BELOW: when the PC stands on the
dungeon entrance node, the router must be told the entrance has a surface exit —
otherwise a 'back up the rope' intent is never classified as movement and the
party is stranded.

The surface owner is derivable from cartography alone: the entrance node was
created by the ``the_dropmouth`` → ``deep_descent`` route, so the region a PC
ascends to is that route's ``from_id`` (the_dropmouth, display 'The Dropmouth').

RED until 105-3 lands: today the entrance node is not a cartography region, so
the projection lists no exits for it.
"""

from __future__ import annotations

import types

import pytest

from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
)
from sidequest.server.intent_router_pass import _build_state_summary

# ---------------------------------------------------------------------------
# Cartography helpers — beneath_sunden-shaped (mirrors the descent projection).
# ---------------------------------------------------------------------------


def _hybrid_cartography() -> CartographyConfig:
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
    )


def _oz_cartography() -> CartographyConfig:
    """Region-mode world with NO registered seam route — no surface owner."""
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
        routes=[],
    )


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(rules=None, witnessed_acts=None, worlds={world_slug: world})


def _snapshot(world_slug: str, genre_slug: str, region: str) -> GameSnapshot:
    return GameSnapshot(
        genre_slug=genre_slug,
        world_slug=world_slug,
        pc_regions={"Groucho": region},
        player_seats={"p1": "Groucho"},
    )


class _Kit:
    def __init__(self, snapshot, pack):
        self.snapshot = snapshot
        self.pack = pack


@pytest.fixture
def deep_world_kit():
    """PC at the dungeon entrance node of beneath_sunden (a hybrid seam world)."""
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    return _Kit(_snapshot("beneath_sunden", "caverns_and_claudes", ENTRANCE_ID), pack)


@pytest.fixture
def deep_oz_kit():
    """PC at the entrance node of a world with no registered seam route."""
    cart = _oz_cartography()
    pack = _pack_with_cartography("oz", cart)
    return _Kit(_snapshot("oz", "wry_whimsy", ENTRANCE_ID), pack)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_entrance_node_lists_surface_exit(deep_world_kit):
    """AC3: at the entrance node, the projection lists the surface ascent exit so
    the router classifies a 'back up the rope' intent as movement."""
    kit = deep_world_kit
    summary = _build_state_summary(kit.snapshot, pack=kit.pack)
    assert "current_region_exits" in summary, (
        "entrance node must project its exits so departure intents route as movement"
    )
    exits = {(e["name"], e["kind"]) for e in summary["current_region_exits"]}
    assert ("The Dropmouth", "seam") in exits, (
        f"expected a seam exit back to the surface owner 'The Dropmouth', got: {exits}"
    )


def test_no_seam_world_entrance_has_no_surface_exit(deep_oz_kit):
    """No silent fallback: a world with no registered seam route owns no surface
    crossing, so the entrance node must not project a fabricated seam exit."""
    kit = deep_oz_kit
    summary = _build_state_summary(kit.snapshot, pack=kit.pack)
    seam_exits = [
        e for e in summary.get("current_region_exits", []) if e.get("kind") == "seam"
    ]
    assert seam_exits == [], (
        f"no-seam world must not invent a surface exit at the entrance, got: {seam_exits}"
    )
