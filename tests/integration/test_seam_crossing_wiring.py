"""Story 105-2 wiring: the seam crossing is reachable from PRODUCTION paths
— the dispatch-bank door AND the narration-guard door — and the seam
registry is populated at import time.

Tasks 1-5 proved the crossing in isolation (``run_movement_dispatch`` and
``_apply_narration_result_to_snapshot`` called directly). This file proves
the same crossing is *wired*: it survives the real production choke points.

* ``run_dispatch_bank`` is the bank ``movement.py`` is registered into and
  the bank ``execute_intent_router_pre_narrator_pass`` drives before the
  narrator. Crossing through *it* (not ``run_movement_dispatch`` directly)
  proves the movement subsystem is registered, reachable, and receives the
  ``snapshot``/``pack``/``dungeon_store``/``palette`` context the production
  caller threads.
* ``_apply_narration_result_to_snapshot`` is the narration-apply guard the
  production handler calls; threading ``lookahead_handle`` through it proves
  the recovery door is reachable end-to-end.
* ``_REGISTRY["deep_descent"]`` proves the seam resolver is bound at import
  time — the dispatch bank and the narration guard both resolve the crossing
  through this registry, so an empty registry would dead-code both doors.

The scaffolding (cartography/store/snapshot doubles, authored entrance room)
mirrors ``tests/agents/subsystems/test_movement_seam_crossing.py`` and
``tests/server/test_narration_seam_recovery.py`` — same shapes, driven one
production layer up.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from sidequest.agents.subsystems import run_dispatch_bank
from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
)
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# Re-export the otel_capture fixture (global InMemorySpanExporter) — spans
# opened via Span.open land on the global provider, which otel_capture taps.
from tests.server.conftest import otel_capture  # noqa: F401

ENTRANCE_ROOM_NAME = "Under the Rope"


# ---------------------------------------------------------------------------
# Store / palette doubles (same shape as the Task 1-5 suites)
# ---------------------------------------------------------------------------


class _StoreWithEntrance:
    """DungeonStore double: load_map returns a graph with the entrance node."""

    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar"))
        return g


class _FakePalette:
    """Duck-typed ThemePalette — the surface→deep crossing never reaches
    projection, but the movement signature wants one."""

    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


class _FakeLookaheadHandle:
    """Minimal LookaheadWorkerHandle double carrying persistence + slugs."""

    def __init__(self, store, *, genre_slug: str, world_slug: str):
        self.persistence = store
        self.genre_slug = genre_slug
        self.world_slug = world_slug


# ---------------------------------------------------------------------------
# Cartography / pack / snapshot helpers (beneath_sunden-shaped: region-mode
# with a registered seam route owned by the_dropmouth → deep_descent)
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


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    """Duck-typed GenrePack: pack.worlds[slug].cartography + rules (read by
    run_dispatch_bank._threshold_for; empty dict → 0.6 default, which the
    confidence=1.0 dispatch clears so the movement subsystem engages)."""
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(
        worlds={world_slug: world},
        rules=types.SimpleNamespace(dispatch_confidence_thresholds={}),
    )


def _hybrid_snapshot() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Groucho": "the_dropmouth"},
        player_seats={"p1": "Groucho"},
    )
    snap.current_region = "the_dropmouth"
    snap.character_locations["Groucho"] = "The Dropmouth"
    return snap


def _build_authored_world(tmp_path: Path, monkeypatch) -> None:
    """Author ``rooms/entrance.yaml`` under a tmp tree and point the loader's
    module-level search-path constant at it, so the recovery re-anchor
    resolves the authored name 'Under the Rope' (mirrors the Task 5 fixture)."""
    rooms_dir = tmp_path / "caverns_and_claudes" / "worlds" / "beneath_sunden" / "rooms"
    rooms_dir.mkdir(parents=True)
    (rooms_dir / f"{ENTRANCE_ID}.yaml").write_text(
        "room_type: settlement\n"
        f"name: {ENTRANCE_ROOM_NAME}\n"
        "description: The shaft collar where the rope ends.\n"
    )
    monkeypatch.setattr(
        "sidequest.genre.loader.DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [tmp_path],
    )


def _movement_package() -> DispatchPackage:
    return DispatchPackage(
        turn_id="seam-wiring-turn",
        per_player=[
            PlayerDispatch(
                player_id="Groucho",
                raw_action="I climb down the rope into the dark.",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="movement",
                        params={"direction": "deeper", "exit_descriptor": "down the rope"},
                        idempotency_key="seam-wiring-mv",
                        confidence=1.0,
                        visibility=VisibilityTag(visible_to="all"),
                    )
                ],
            )
        ],
        cross_player=[],
        confidence_global=1.0,
    )


# ---------------------------------------------------------------------------
# Door 0: the registry is populated at import time
# ---------------------------------------------------------------------------


def test_deep_descent_registered_at_import():
    """Both production doors resolve the crossing through this registry; an
    empty registry dead-codes both. Importing the module must bind it."""
    from sidequest.game.seams.registry import _REGISTRY

    assert "deep_descent" in _REGISTRY


# ---------------------------------------------------------------------------
# Door 1: the dispatch bank reaches the crossing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_bank_reaches_the_crossing():
    """Through ``run_dispatch_bank`` — the production bank ``movement.py`` is
    registered into — not ``run_movement_dispatch`` directly. The PC ends at
    the procedural entrance, proving the bank threaded the seam context."""
    snapshot = _hybrid_snapshot()
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    result = await run_dispatch_bank(
        _movement_package(),
        context={
            "snapshot": snapshot,
            "pack": pack,
            "player_name": "Groucho",
            "dungeon_store": _StoreWithEntrance(),
            "palette": _FakePalette(),
        },
    )

    # The movement subsystem ENGAGED (not degraded-to-hint): its output is
    # recorded under the dispatch's idempotency_key.
    assert "seam-wiring-mv" in result.outputs_by_key, (
        f"movement dispatch did not engage through the bank; "
        f"outputs={list(result.outputs_by_key)} errors={result.errors}"
    )
    out = result.outputs_by_key["seam-wiring-mv"]
    assert out.data.get("resolved_via") == "surface_descent", (
        f"expected surface_descent crossing through the bank, got: {out.data}"
    )
    assert out.data.get("to_region") == ENTRANCE_ID, (
        f"expected to_region={ENTRANCE_ID!r}, got: {out.data.get('to_region')!r}"
    )
    # The canonical snapshot was mutated by the production path.
    assert snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        f"PC not rebound to entrance through the bank; "
        f"still at {snapshot.region_for(perspective='Groucho')!r}"
    )


# ---------------------------------------------------------------------------
# Door 2: the narration-apply guard reaches the crossing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_pipeline_reaches_the_guard(tmp_path, monkeypatch):
    """Through ``_apply_narration_result_to_snapshot`` with ``lookahead_handle``
    threaded — the narration-guard recovery door. An unresolvable heading on a
    seam-owning region performs the REAL crossing."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    _build_authored_world(tmp_path, monkeypatch)
    snapshot = _hybrid_snapshot()
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())
    handle = _FakeLookaheadHandle(
        _StoreWithEntrance(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )

    result = NarrationTurnResult(
        narration="The rope ends and the dark swallows you whole.",
        location="The Deep Below",  # unresolvable heading → recovery door
    )
    _apply_narration_result_to_snapshot(
        snapshot=snapshot,
        result=result,
        player_name="Groucho",
        room=room_for(snapshot=snapshot),
        pack=pack,
        world="beneath_sunden",
        lookahead_handle=handle,
    )

    assert snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        f"PC not rebound to entrance through the narration guard; "
        f"still at {snapshot.region_for(perspective='Groucho')!r}"
    )
    # The confabulated heading must NOT pollute the surface graph.
    assert "The Deep Below" not in snapshot.discovered_regions
    # The scene re-anchored to the authored entrance room name.
    assert result.location == ENTRANCE_ROOM_NAME, (
        f"result.location must be the authored room name {ENTRANCE_ROOM_NAME!r}; "
        f"got {result.location!r}"
    )


# ---------------------------------------------------------------------------
# Door 1 OTEL proof: the crossing fired the seam resolver, not improvisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_bank_crossing_emits_seam_resolved_span(otel_capture):  # noqa: F811
    """The lie-detector check: the bank crossing emits exactly one
    ``movement.resolved`` span carrying ``resolved_via=surface_descent`` and
    ``seam_kind=deep_descent`` — proof the seam resolver fired, not a narrator
    improvisation. Mirrors the span assertion in the Task 1 movement suite,
    captured one production layer up (through the bank)."""
    snapshot = _hybrid_snapshot()
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    await run_dispatch_bank(
        _movement_package(),
        context={
            "snapshot": snapshot,
            "pack": pack,
            "player_name": "Groucho",
            "dungeon_store": _StoreWithEntrance(),
            "palette": _FakePalette(),
        },
    )

    resolved = [s for s in otel_capture.get_finished_spans() if s.name == "movement.resolved"]
    assert len(resolved) == 1, (
        f"expected exactly one movement.resolved span for the bank crossing, "
        f"got {len(resolved)}: {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = resolved[0].attributes or {}
    assert attrs.get("resolved_via") == "surface_descent", (
        f"span must carry resolved_via=surface_descent; got {attrs.get('resolved_via')!r}"
    )
    assert attrs.get("seam_kind") == "deep_descent", (
        f"span must carry seam_kind=deep_descent; got {attrs.get('seam_kind')!r}"
    )
