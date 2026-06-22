"""Affordance race fix (2026-06-22): a resolved dungeon move must COMMIT the
destination's onward ring BEFORE the dispatch returns — generation-before-narrate.

Root cause (live trace, beneath_sunden turn 2 → exp002.r2): the per-PC region
patch fires the §Q3 next-ring look-ahead as a BACKGROUND create_task, so the
onward exits materialized 2ms–7s AFTER the narrator's prompt was built. The
narrator described a room whose forward exits did not exist yet and the player
was shown a dead-end. The fix awaits ``lookahead_handle.drain()`` after a move
resolves, so the onward ring is on the graph before narration.

These tests pin: (1) a resolved navigator move drains the look-ahead and the
onward ring is present at return time; (2) the ``movement.resolved`` span carries
``onward_ring_drained=True`` (the lie-detector for the fix); (3) with no
look-ahead handle the move still resolves (drain is a no-op, not a hard dep).
"""

from __future__ import annotations

import asyncio
import types

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region, Route
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


def _run(coro):
    return asyncio.run(coro)


def _movement(direction: str, descriptor: str = "") -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"direction": direction, "exit_descriptor": descriptor},
        idempotency_key="mv-onward",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


class _DeferredOnwardStore:
    """DungeonStore double whose ONWARD ring (beyond the destination) appears only
    after the look-ahead drains — mirroring the real background materialize being
    awaited to completion. Before drain: entrance + exp001.r0 (the destination,
    pre-materialized). After drain: + exp001.r0 → exp002.r0 (the onward ring)."""

    def __init__(self):
        self.onward_ready = False

    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(
            RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar", depth_score=0.0)
        )
        g.add_node(RegionNode(id="exp001.r0", expansion_id=1, theme="cavern", depth_score=10.0))
        g.add_edge(RegionEdge(a=entrance_id, b="exp001.r0", kind="shaft"))
        if self.onward_ready:
            g.add_node(RegionNode(id="exp002.r0", expansion_id=2, theme="cavern", depth_score=20.0))
            g.add_edge(RegionEdge(a="exp001.r0", b="exp002.r0", kind="corridor"))
        return g


class _FakeLookahead:
    """LookaheadWorkerHandle double: drain() commits the destination's onward ring,
    standing in for the real background create_task being awaited to completion."""

    def __init__(self, store: _DeferredOnwardStore):
        self._store = store
        self.drained = False

    async def drain(self) -> None:
        self.drained = True
        self._store.onward_ready = True


class _FakePalette:
    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


def _hybrid_cartography() -> CartographyConfig:
    return CartographyConfig(
        starting_region="ropefoot",
        navigation_mode=NavigationMode.region,
        regions={
            "ropefoot": Region(name="Ropefoot", summary="Surface camp.", description="The camp."),
            "the_dropmouth": Region(
                name="The Dropmouth", summary="The lip.", description="The shaft mouth."
            ),
        },
        routes=[
            Route(
                name="Down the Rope",
                description="d",
                from_id="the_dropmouth",
                to_id="deep_descent",
            ),
        ],
    )


def _pack(world_slug: str, cart: CartographyConfig):
    world = types.SimpleNamespace(cartography=cart)
    return types.SimpleNamespace(worlds={world_slug: world})


def _snapshot() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Groucho": ENTRANCE_ID},
        player_seats={"p1": "Groucho"},
    )
    snap.discovered_regions.append(ENTRANCE_ID)
    return snap


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-movement-onward-ring")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def test_resolved_move_drains_onward_ring_before_returning(capture_spans):
    """A resolved navigator move (entrance → exp001.r0) drains the look-ahead, so
    the destination's onward ring (exp002.r0) is on the graph BEFORE the dispatch
    returns — i.e. the exits exist before the narrator describes the room."""
    store = _DeferredOnwardStore()
    handle = _FakeLookahead(store)
    snap = _snapshot()

    # Pre-condition: the onward ring does NOT exist yet (background, undrained).
    assert "exp002.r0" not in store.load_map(entrance_id=ENTRANCE_ID).nodes

    out = _run(
        run_movement_dispatch(
            _movement("deeper", "deeper into the dark"),
            snapshot=snap,
            player_name="Groucho",
            dungeon_store=store,
            palette=_FakePalette(),
            pack=_pack("beneath_sunden", _hybrid_cartography()),
            lookahead_handle=handle,
        )
    )

    # The move resolved into the dungeon (not deferred / unresolved).
    assert out.data.get("to_region") == "exp001.r0", out.data
    # The fix engaged: the look-ahead was drained...
    assert handle.drained is True, "run_movement_dispatch did not drain the look-ahead"
    # ...and the onward ring is now COMMITTED on the graph at return time.
    assert "exp002.r0" in store.load_map(entrance_id=ENTRANCE_ID).nodes, (
        "onward ring not materialized before dispatch returned — the narrator would "
        "describe a dead-end room (the affordance race)"
    )


def test_resolved_move_span_records_onward_ring_drained(capture_spans):
    """The movement.resolved span carries onward_ring_drained=True — the
    lie-detector proving generation-before-narrate engaged (GM panel visible)."""
    store = _DeferredOnwardStore()
    handle = _FakeLookahead(store)
    _run(
        run_movement_dispatch(
            _movement("deeper", "deeper into the dark"),
            snapshot=_snapshot(),
            player_name="Groucho",
            dungeon_store=store,
            palette=_FakePalette(),
            pack=_pack("beneath_sunden", _hybrid_cartography()),
            lookahead_handle=handle,
        )
    )
    resolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.resolved"]
    assert len(resolved) == 1, "expected exactly one movement.resolved span"
    assert (resolved[0].attributes or {}).get("onward_ring_drained") is True


def test_resolved_move_without_lookahead_handle_still_resolves(capture_spans):
    """drain is a no-op when there is no look-ahead handle — the move still
    resolves (the fix is additive, never a hard dependency / fail-loud path)."""
    store = _DeferredOnwardStore()
    out = _run(
        run_movement_dispatch(
            _movement("deeper", "deeper into the dark"),
            snapshot=_snapshot(),
            player_name="Groucho",
            dungeon_store=store,
            palette=_FakePalette(),
            pack=_pack("beneath_sunden", _hybrid_cartography()),
            lookahead_handle=None,
        )
    )
    assert out.data.get("to_region") == "exp001.r0", out.data
    resolved = [s for s in capture_spans.get_finished_spans() if s.name == "movement.resolved"]
    assert (resolved[0].attributes or {}).get("onward_ring_drained") is False
