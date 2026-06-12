"""current_region_exits projection (Story 105-2 Piece 2, 59-27 pattern).

Root cause #2 of epic 105: the movement classification prompt defines movement
as relocation "between dungeon regions", and the router's state summary never
mentions that the current region's one onward exit is a descent named "Down the
Rope" (a registered seam route). The router was asked to recognize a descent it
was never told existed. This projection is the lexical bridge: it surfaces the
PC's current cartography region's REAL exits (adjacency neighbors + seam routes)
into the state summary the router sees, and emits an OTEL span when it fires.
"""

from __future__ import annotations

import types

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
)
from sidequest.server.intent_router_pass import _build_state_summary

# ---------------------------------------------------------------------------
# Cartography helpers — beneath_sunden-shaped (mirrors Task 3 fixtures).
# ---------------------------------------------------------------------------


def _hybrid_cartography() -> CartographyConfig:
    """Region-mode world with a registered seam route AND adjacency.

    ``the_dropmouth`` owns the ``Down the Rope`` route to ``deep_descent`` (a
    registered seam kind) and is adjacent to ``ropefoot``; ``ropefoot`` is
    adjacent back to ``the_dropmouth`` (so the "adjacent" kind is testable).
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


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    """Duck-typed GenrePack exposing only what _build_state_summary reads.

    ``rules=None`` skips the confrontation block; ``witnessed_acts=None`` skips
    the witnessed-act block; ``worlds[slug].cartography`` feeds the new
    region-exits projection.
    """
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
        pc_regions={"Groucho": region},
        player_seats={"p1": "Groucho"},
    )


class _Kit:
    def __init__(self, snapshot, pack):
        self.snapshot = snapshot
        self.pack = pack


@pytest.fixture
def hybrid_world_kit():
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    return _Kit(_snapshot("the_dropmouth"), pack)


@pytest.fixture
def hybrid_world_kit_at_ropefoot():
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    return _Kit(_snapshot("ropefoot"), pack)


@pytest.fixture
def plain_snapshot_and_pack():
    """A pack/world with no cartography at all → no projection."""
    snap = _snapshot("nowhere")
    world = types.SimpleNamespace(cartography=None)
    pack = types.SimpleNamespace(rules=None, witnessed_acts=None, worlds={"beneath_sunden": world})
    return snap, pack


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_seam_region_summary_names_the_exit(hybrid_world_kit):
    kit = hybrid_world_kit  # PC at the_dropmouth
    summary = _build_state_summary(kit.snapshot, pack=kit.pack)
    exits = summary["current_region_exits"]
    assert {"name": "Down the Rope", "kind": "seam"} in exits


def test_adjacent_regions_listed(hybrid_world_kit_at_ropefoot):
    kit = hybrid_world_kit_at_ropefoot
    summary = _build_state_summary(kit.snapshot, pack=kit.pack)
    kinds = {(e["name"], e["kind"]) for e in summary["current_region_exits"]}
    assert ("The Dropmouth", "adjacent") in kinds  # cart region's display name


def test_no_cartography_no_projection(plain_snapshot_and_pack):
    snapshot, pack = plain_snapshot_and_pack
    summary = _build_state_summary(snapshot, pack=pack)
    assert "current_region_exits" not in summary


def test_no_pack_no_projection():
    snap = _snapshot("the_dropmouth")
    summary = _build_state_summary(snap)  # pack=None
    assert "current_region_exits" not in summary


def test_projection_emits_span(otel_capture, hybrid_world_kit):
    kit = hybrid_world_kit
    _build_state_summary(kit.snapshot, pack=kit.pack)
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "intent_router.region_exits"]
    assert len(spans) == 1, "expected exactly one region_exits span"
    attrs = spans[0].attributes or {}
    # the_dropmouth: one adjacent (ropefoot) + one seam (Down the Rope)
    assert attrs.get("exit_count") == 2
    assert attrs.get("seam_count") == 1
    assert attrs.get("region_id") == "the_dropmouth"
    assert attrs.get("genre_slug") == "caverns_and_claudes"
