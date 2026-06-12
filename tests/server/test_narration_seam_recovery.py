"""Seam recovery at the narration guard (Story 105-2 Task 5, AC1/AC2).

The turn-3 repro: router emitted NO movement dispatch; the narrator patched
location to "The Dropmouth — The Deep" (a confabulated deep). The guard must
treat that patch as the missed relocation signal: perform the REAL crossing,
or fail loud — never accept the confabulation.

Fixture shapes:
- ``hybrid_apply_kit``       — beneath_sunden-shaped: region-mode + seam + live store.
- ``hybrid_apply_kit_empty_store`` — same, but store has no entrance node (corrupt/uninitialized).
- ``oz_apply_kit``           — wry_whimsy/oz-shaped: region-mode, NO seam routes (90-6 non-regression).

Apply-call shape mirrors ``test_region_drift_encounter_continue.py``'s ``_apply()``
helper. Span assertions use the ``otel_capture`` fixture from conftest.py.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
)
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# Store doubles (identical to test_movement_seam_crossing.py)
# ---------------------------------------------------------------------------


class _StoreWithEntrance:
    """DungeonStore double: load_map returns a graph with the entrance node."""

    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar"))
        return g


class _EmptyStore:
    """DungeonStore double: load_map returns a graph with NO nodes (corrupt seed)."""

    def load_map(self, *, entrance_id):
        return RegionGraph(entrance_id=entrance_id)


# ---------------------------------------------------------------------------
# Cartography helpers
# ---------------------------------------------------------------------------


def _hybrid_cartography() -> CartographyConfig:
    """beneath_sunden-shaped: region-mode with a registered seam route.

    the_dropmouth owns a route to ``deep_descent`` (a registered seam kind),
    so a confabulated "The Dropmouth — The Deep" heading should trigger
    the seam-recovery guard.
    """
    return CartographyConfig(
        starting_region="ropefoot",
        navigation_mode=NavigationMode.region,
        regions={
            "ropefoot": Region(
                name="Ropefoot",
                summary="Surface camp.",
                description="The waiting camp above the shaft.",
            ),
            "the_dropmouth": Region(
                name="The Dropmouth",
                summary="The lip of the shaft.",
                description="The mouth of the descent.",
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
    """wry_whimsy/oz-shaped: region-mode, NO seam routes."""
    return CartographyConfig(
        starting_region="emerald_city",
        navigation_mode=NavigationMode.region,
        regions={
            "emerald_city": Region(
                name="Emerald City",
                summary="The green hub.",
                description="A shimmering emerald hub.",
            ),
        },
    )


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    """Duck-typed GenrePack: exposes pack.worlds[slug].cartography."""
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(worlds={world_slug: world})


# ---------------------------------------------------------------------------
# LookaheadWorkerHandle double
# ---------------------------------------------------------------------------


class _FakeLookaheadHandle:
    """Minimal LookaheadWorkerHandle double carrying persistence + slugs."""

    def __init__(self, store, *, genre_slug: str = "", world_slug: str = ""):
        self.persistence = store
        self.genre_slug = genre_slug
        self.world_slug = world_slug


# ---------------------------------------------------------------------------
# Apply kit helper class
# ---------------------------------------------------------------------------


class _ApplyKit:
    """All the moving parts for a narration seam-recovery test.

    ``apply(result)`` wraps ``_apply_narration_result_to_snapshot`` the same
    way the production handler calls it (lookahead_handle threaded), so the
    test only states the input and asserts the output.
    """

    def __init__(
        self,
        snapshot: GameSnapshot,
        pack,
        player_name: str,
        world_slug: str,
        handle: _FakeLookaheadHandle | None,
        captured_spans,
    ):
        self.snapshot = snapshot
        self.pack = pack
        self.player_name = player_name
        self.world_slug = world_slug
        self.handle = handle
        self._captured_spans = captured_spans

    def narration_result(self, *, location: str) -> NarrationTurnResult:
        return NarrationTurnResult(
            narration="The rope ends and the dark swallows you whole.",
            location=location,
        )

    def apply(self, result: NarrationTurnResult) -> None:
        _apply_narration_result_to_snapshot(
            snapshot=self.snapshot,
            result=result,
            player_name=self.player_name,
            room=room_for(snapshot=self.snapshot),
            pack=self.pack,
            world=self.world_slug,
            lookahead_handle=self.handle,
        )

    def assert_span(self, span_name: str, **attrs: Any) -> None:
        """Assert at least one finished span with ``span_name`` + matching attrs."""
        finished = self._captured_spans.get_finished_spans()
        matching = [s for s in finished if s.name == span_name]
        assert matching, (
            f"Expected span {span_name!r} but none found. "
            f"Finished spans: {[s.name for s in finished]}"
        )
        for key, expected_val in attrs.items():
            attr_hits = [s for s in matching if (s.attributes or {}).get(key) == expected_val]
            assert attr_hits, (
                f"Span {span_name!r} found but no instance had "
                f"{key}={expected_val!r}. "
                f"Seen attrs: {[(s.attributes or {}) for s in matching]}"
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hybrid_apply_kit(otel_capture):
    """beneath_sunden-shaped apply kit: hybrid cartography + live store."""
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Groucho": "the_dropmouth"},
        player_seats={"p1": "Groucho"},
    )
    snap.character_locations["Groucho"] = "The Dropmouth"
    handle = _FakeLookaheadHandle(
        _StoreWithEntrance(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )
    return _ApplyKit(snap, pack, "Groucho", "beneath_sunden", handle, otel_capture)


@pytest.fixture
def hybrid_apply_kit_empty_store(otel_capture):
    """beneath_sunden-shaped apply kit: hybrid cartography + dead store (no entrance node)."""
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Groucho": "the_dropmouth"},
        player_seats={"p1": "Groucho"},
    )
    snap.character_locations["Groucho"] = "The Dropmouth"
    handle = _FakeLookaheadHandle(
        _EmptyStore(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )
    return _ApplyKit(snap, pack, "Groucho", "beneath_sunden", handle, otel_capture)


@pytest.fixture
def oz_apply_kit(otel_capture):
    """oz-shaped apply kit: region-mode world with NO seam routes (90-6 non-regression)."""
    cart = _oz_cartography()
    pack = _pack_with_cartography("oz", cart)
    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        pc_regions={"Groucho": "emerald_city"},
        player_seats={"p1": "Groucho"},
    )
    snap.character_locations["Groucho"] = "Emerald City"
    snap.current_region = "emerald_city"
    return _ApplyKit(snap, pack, "Groucho", "oz", None, otel_capture)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unresolved_heading_on_seam_region_recovers_crossing(hybrid_apply_kit):
    """AC1: confabulated deep heading triggers real seam crossing.

    The turn-3 repro shape: router missed the descent; the narrator emitted a
    heading that does NOT resolve to any known cartography region (e.g. "The Deep
    Below Sunden" — a confabulated procedural deep). The guard detects the PC is
    on a seam region (the_dropmouth) and performs the REAL crossing — PC ends up
    at the procedural entrance, not stuck at the_dropmouth.

    Note: the repro heading must NOT resolve to the_dropmouth via the leading
    segment, otherwise the code hits the same-region-drift branch first.
    "The Deep Below" has no match in the fixture cartography.
    """
    kit = hybrid_apply_kit
    result = kit.narration_result(location="The Deep Below")
    kit.apply(result)

    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        f"PC must be rebound to entrance after seam recovery; "
        f"still at {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    # The confabulated heading must NOT pollute the surface graph.
    assert "The Deep Below" not in kit.snapshot.discovered_regions, (
        "confabulated deep heading must never enter discovered_regions"
    )


def test_recovery_reanchors_scene_to_authored_room_name(hybrid_apply_kit):
    """AC2: after recovery, result.location is the real entrance region id (not the confabulation).

    The fixture has no rooms/<entrance>.yaml on the fixture pack path, so
    ``_entrance_room_name`` degrades loudly to the raw region id — that is
    the designed loud-skip path for a missing authored room, and the assertion
    checks the degraded-but-honest title rather than a fabricated one.
    """
    kit = hybrid_apply_kit
    result = kit.narration_result(location="The Deep Below")
    kit.apply(result)

    # result.location must be re-anchored away from the confabulation.
    # With no authored rooms/<entrance>.yaml in fixture packs, the helper
    # degrades to the region id — still not the confabulation.
    assert result.location != "The Deep Below", (
        "result.location must be re-anchored after a successful seam recovery; "
        f"got {result.location!r}"
    )
    assert result.location != "", (
        "result.location must not be emptied on a successful recovery (crossing performed)"
    )


def test_dead_store_rejects_patch_loud(hybrid_apply_kit_empty_store, otel_capture):
    """AC3: dead store (corrupt dungeon) → patch rejected loudly, PC stays put.

    The store has no entrance node — SeamCrossingError fires with
    ``reason="no_dungeon_entrance"``. The guard must:
    - Drop the location patch (PC stays at the_dropmouth).
    - NOT pollute discovered_regions.
    - Emit ``region.entry_rejected`` with ``reason="seam_crossing_unresolvable"``.
    """
    kit = hybrid_apply_kit_empty_store
    result = kit.narration_result(location="The Deep Below")
    kit.apply(result)

    assert kit.snapshot.region_for(perspective="Groucho") == "the_dropmouth", (
        "PC must stay at the_dropmouth when seam crossing fails (no silent fallback)"
    )
    assert "The Deep Below" not in kit.snapshot.discovered_regions, (
        "confabulated heading must NOT enter discovered_regions even on failure"
    )
    kit.assert_span("region.entry_rejected", reason="seam_crossing_unresolvable")


def test_seamless_region_mode_world_unchanged(oz_apply_kit):
    """90-6 non-regression: a POI re-title in a seam-less region-mode world
    still hits entry_skipped_sub_location (reason=sub_location_in_region_mode_world)
    and same-region drift. The seam-recovery guard must NOT fire.

    Uses "The Throne Room" — a POI within Emerald City that does NOT resolve to
    any cartography region, reaching the elif _is_region_mode_world branch.
    """
    kit = oz_apply_kit
    result = kit.narration_result(location="The Throne Room")
    kit.apply(result)

    # PC stays in the oz region (no seam to cross).
    assert kit.snapshot.region_for(perspective="Groucho") == "emerald_city", (
        f"PC region must stay emerald_city in a seam-less world; "
        f"got {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    # The unresolved POI heading must not pollute the graph.
    assert "The Throne Room" not in kit.snapshot.discovered_regions

    # The EXISTING 90-6 branch fires (not the new seam-recovery guard).
    kit.assert_span("region.entry_rejected", reason="sub_location_in_region_mode_world")
