"""Task 6 (ADR-116): region-keyed co-location — _co_located helper + span.

TDD:
  Steps 1–7: helper unit tests (no DB, no pack loading).
  Step 10: integration test for the confrontation.colocation span emitted
           by instantiate_encounter_from_trigger.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import Npc
from sidequest.server.dispatch.encounter_lifecycle import _co_located


def _npc(name, *, region=None, last_seen=None):
    n = Npc(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            level=1,
            xp=0,
            hp=HpPool(current=6, max=6, base_max=6),
        )
    )
    n.region = region
    n.last_seen_location = last_seen
    return n


def test_region_match_seats_when_regions_equal():
    npc = _npc("Gnaw-Swarm", region="exp002.r3", last_seen="Under the Rope")
    # Scene mismatch ("Under the Rope" != PC scene) but region matches → co-located.
    assert _co_located(npc, pc_region="exp002.r3", scene_match=False) is True


def test_region_mismatch_blocks_even_if_scene_would_match():
    npc = _npc("Gnaw-Swarm", region="exp004.r1")
    assert _co_located(npc, pc_region="exp002.r3", scene_match=True) is False


def test_regionless_npc_falls_back_to_scene_match():
    npc = _npc("Innkeeper", region=None)
    assert _co_located(npc, pc_region="exp002.r3", scene_match=True) is True
    assert _co_located(npc, pc_region="exp002.r3", scene_match=False) is False


def test_no_pc_region_falls_back_to_scene_match():
    npc = _npc("Gnaw-Swarm", region="exp002.r3")
    assert _co_located(npc, pc_region=None, scene_match=True) is True


# ---------------------------------------------------------------------------
# Step 10: confrontation.colocation span integration test
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def test_confrontation_colocation_span_emitted_with_scene_mode(otel_capture):
    """The confrontation.colocation span fires on every instantiate_encounter_from_trigger
    call. When the PC has no region (pc_regions not set), match_mode is 'scene'.
    """
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.genre.loader import load_genre_pack
    from tests._helpers.trigger_encounter import trigger_encounter

    _FIXTURE_PACK = Path(__file__).resolve().parents[2] / "fixtures" / "packs" / "test_genre"
    pack = load_genre_pack(_FIXTURE_PACK)

    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.character_locations["Hero"] = "Dungeon Corridor"
    # No pc_regions entry → region_for returns None → match_mode="scene"
    # Add a hostile NPC at the same location so opponent seating succeeds
    hostile_npc = Npc(
        core=CreatureCore(
            name="Cave Rat",
            description="A vicious rodent.",
            personality="aggressive",
            level=1,
            xp=0,
            hp=HpPool(current=6, max=6, base_max=6),
        ),
        npc_role_id="hostile",
        last_seen_location="Dungeon Corridor",
        last_seen_turn=1,
    )
    snap.npcs.append(hostile_npc)

    trigger_encounter(snap, pack, "combat", "Hero", npcs_present=[])

    colocation_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "confrontation.colocation"
    ]
    assert colocation_spans, (
        "confrontation.colocation span never fired; "
        f"all spans: {[s.name for s in otel_capture.get_finished_spans()]!r}"
    )
    attrs = dict(colocation_spans[0].attributes or {})
    assert attrs.get("match_mode") == "scene", (
        f"when PC has no region, match_mode must be 'scene'; got {attrs.get('match_mode')!r}"
    )
    assert "encounter_type" in attrs, "span must carry encounter_type"
    assert "opponent_count" in attrs, "span must carry opponent_count"


def test_region_mode_seating_integration(otel_capture):
    """Region-stamped creature seats a confrontation via region co-location only.

    End-to-end path: pc_regions is set → region_for returns a region id →
    _npc_fallback_at_location calls _co_located with that region → the creature's
    matching npc.region passes the gate even though last_seen_location does NOT
    match the PC's character_locations entry.

    This is the keystone integration test for Task 6 / ADR-116 region-keyed
    seating: proves that region (not scene) seated the creature.
    """
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.genre.loader import load_genre_pack
    from tests._helpers.trigger_encounter import trigger_encounter

    _FIXTURE_PACK = Path(__file__).resolve().parents[2] / "fixtures" / "packs" / "test_genre"
    pack = load_genre_pack(_FIXTURE_PACK)

    _REGION = "exp002.r3"
    _PC_SCENE = "Dungeon Corridor"
    # The creature's last_seen_location is deliberately different from _PC_SCENE
    # so that ONLY the region match (not scene match) can seat it.
    _CREATURE_STALE_SCENE = "Under the Rope Bridge"

    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=3),
    )
    # PC has a known scene location AND a region stamp.
    snap.character_locations["Hero"] = _PC_SCENE
    snap.pc_regions["Hero"] = _REGION

    # Region-stamped hostile creature whose last_seen_location does NOT match the
    # PC's scene — this creature can ONLY be seated via region co-location, not
    # via the free-text scene fallback (_co_located short-circuits on npc.region
    # when both pc_region and npc.region are set, ignoring scene_match entirely).
    region_creature = Npc(
        core=CreatureCore(
            name="Gnaw-Swarm",
            description="A mass of biting insects.",
            personality="aggressive",
            level=1,
            xp=0,
            hp=HpPool(current=6, max=6, base_max=6),
        ),
        npc_role_id="hostile",
        last_seen_location=_CREATURE_STALE_SCENE,
        last_seen_turn=1,
    )
    region_creature.region = _REGION
    snap.npcs.append(region_creature)

    # Must not raise NoOpponentAvailableError — the region match seats the creature.
    trigger_encounter(snap, pack, "combat", "Hero", npcs_present=[])

    # The confrontation was instantiated and the creature is a seated opponent.
    enc = snap.encounter
    assert enc is not None, (
        "the confrontation was not instantiated — region-stamped creature was not "
        "seated as the opponent despite matching pc_region"
    )
    opponent_names = {a.name for a in enc.actors if a.side == "opponent"}
    assert "Gnaw-Swarm" in opponent_names, (
        "Gnaw-Swarm must be seated as the opponent (region co-location matched); "
        f"got opponent_names={opponent_names!r}, actors={[(a.name, a.side) for a in enc.actors]!r}"
    )

    # The colocation span must record match_mode="region" (PC has a region).
    colocation_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "confrontation.colocation"
    ]
    assert colocation_spans, (
        "confrontation.colocation span never fired; "
        f"all spans: {[s.name for s in otel_capture.get_finished_spans()]!r}"
    )
    attrs = dict(colocation_spans[0].attributes or {})
    assert attrs.get("match_mode") == "region", (
        "when the PC has a region (pc_regions set), match_mode must be 'region'; "
        f"got {attrs.get('match_mode')!r}"
    )
    assert attrs.get("pc_region") == _REGION, (
        f"span must carry the PC's region id; got {attrs.get('pc_region')!r}"
    )
