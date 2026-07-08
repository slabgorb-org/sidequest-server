"""Track B, Task 3 (Story 164-2): CHARACTERIZATION GUARD.

Pin the CURRENT observable Sünden movement/seam behavior BEFORE the risky Task 6
ladder cutover (movement.py's five-rung inlined seam ladder → SiteRegistry ×
enter_site/exit_site resolvers). These tests PASS on ``develop`` today — a
characterization guard has no RED phase; it locks existing behavior so the Task 6
rewrite can be proven behavior-preserving.

They assert on the OBSERVABLE outcome only (``SubsystemOutput.data`` —
``resolved_via`` / ``to_region``), never on internal rung names. After Task 6 the
seam rungs' ``resolved_via`` migrates to ``site_enter`` / ``site_exit`` while the
destination (``to_region``) is UNCHANGED — this file will be retargeted then, but
until it is, it is the safety net that catches an accidental behavior change.

Modeled on the proven beneath_sunden-shaped doubles in
``tests/agents/subsystems/test_movement_party_split_158_7.py`` (single-PC form
here — no co-mover fan-out). Five rungs:

  1. owned-seam descent   (the_dropmouth → entrance)  resolved_via surface_descent
  2. adjacent-seam descent(ropefoot      → entrance)  resolved_via surface_descent_adjacent
  3. entrance ascent      (entrance → the_dropmouth)  resolved_via surface_ascent
  4. in-dungeon navigation(entrance → exp001.r0)      to_region exp001.r0, no error
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
)
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

# ---------------------------------------------------------------------------
# Doubles (mirror test_movement_party_split_158_7.py / test_movement_onward_ring.py)
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _movement(direction: str, descriptor: str = "") -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"direction": direction, "exit_descriptor": descriptor},
        idempotency_key="mv-164-2-char",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


class _StoreWithEntrance:
    """DungeonStore double: load_map returns a graph with just the entrance node."""

    def load_map(self, *, entrance_id: str) -> RegionGraph:
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar"))
        return g


class _StoreWithDeepGraph:
    """DungeonStore double: entrance + one materialized deep region below it."""

    def load_map(self, *, entrance_id: str) -> RegionGraph:
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
    """beneath_sunden-shaped: region-mode with a registered ``deep_descent`` seam.

    ``the_dropmouth`` OWNS the seam route; ``ropefoot`` (the surface camp) is one
    step adjacent to it. A descent from EITHER crosses the seam onto ``entrance``.
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
    )


def _lateral_cartography() -> CartographyConfig:
    """Two adjacent surface regions and NO seam routes — a pure region-mode world.
    A move whose descriptor matches no adjacent region defers to narration."""
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
# Rung 1 — owned-seam descent from the seam owner.
# ---------------------------------------------------------------------------


def test_owned_seam_descent_from_dropmouth() -> None:
    """PC on the seam-owner region (``the_dropmouth``) descending crosses onto the
    dungeon entrance via the OWNED seam."""
    snap = _snapshot("the_dropmouth")
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "down the rope"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_StoreWithEntrance(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _hybrid_cartography()),
        )
    )
    assert out.data.get("resolved_via") == "surface_descent", out.data
    assert out.data.get("to_region") == ENTRANCE_ID, out.data
    assert snap.pc_regions["Rux"] == ENTRANCE_ID


# ---------------------------------------------------------------------------
# Rung 2 — adjacent-seam descent from one step off the seam.
# ---------------------------------------------------------------------------


def test_adjacent_seam_descent_from_ropefoot() -> None:
    """PC on the surface camp (``ropefoot``, adjacent to the seam owner) descending
    crosses onto the dungeon entrance via the ADJACENT seam — the one-action reach."""
    snap = _snapshot("ropefoot")
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "down the rope"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_StoreWithEntrance(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _hybrid_cartography()),
        )
    )
    assert out.data.get("resolved_via") == "surface_descent_adjacent", out.data
    assert out.data.get("to_region") == ENTRANCE_ID, out.data
    assert snap.pc_regions["Rux"] == ENTRANCE_ID


# ---------------------------------------------------------------------------
# Rung 3 — reverse seam (ascent) from the dungeon entrance.
# ---------------------------------------------------------------------------


def test_entrance_ascent_returns_to_seam_owner() -> None:
    """PC on the dungeon entrance leaving with a non-descent intent ascends back to
    the surface region that OWNS the crossing (``the_dropmouth``)."""
    snap = _snapshot(ENTRANCE_ID)
    out = _run(
        run_movement_dispatch(
            _movement("toward_exit", "back up the rope"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_StoreWithEntrance(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _hybrid_cartography()),
        )
    )
    assert out.data.get("resolved_via") == "surface_ascent", out.data
    assert out.data.get("to_region") == "the_dropmouth", out.data
    assert snap.pc_regions["Rux"] == "the_dropmouth"


# ---------------------------------------------------------------------------
# Rung 4 — in-dungeon graph navigation to a real neighbor (no seam).
# ---------------------------------------------------------------------------


def test_in_dungeon_navigation_steps_to_neighbor() -> None:
    """PC on a materialized dungeon node navigating deeper resolves to a real graph
    neighbor (``exp001.r0``) without error — the §Q1 navigator, not a seam crossing."""
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
# Rung 5 — region-mode lateral defer (unmatched descriptor does not move the PC).
# ---------------------------------------------------------------------------


def test_region_mode_unmatched_descriptor_defers() -> None:
    """A pure region-mode world with an UNMATCHED travel descriptor defers cleanly
    (``region_mode_deferred``) — the PC does not move and the move is NOT an error
    (additive: defer to narration, never fail-loud on a look-around)."""
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
