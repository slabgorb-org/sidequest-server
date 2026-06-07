"""RED wiring test — Story 95-1: region relocation re-centers the orrery.

This is the mandatory integration/wiring test (server CLAUDE.md "No
Source-Text Wiring Tests"): it drives a REAL pc_region relocation through the
production ``_apply_narration_result_to_snapshot`` Site-B seam and asserts the
observable outcome — the party's orbital body and the chart scope re-center on
the new system's star, and an ``orbital.scope_bind`` span fires so the GM panel
can verify it. No grep of source text.

Site B (``narration_apply`` region-mode advance) is the relocation seam that
HAS ``room.session`` in scope (and therefore the orbital content + the scope
setter), so it is the seam this test can drive end-to-end. The cartography
regions are named to match the synthetic sector's star body-ids (identity
join): region ``vorn`` -> star body ``vorn``.

NOTE for Dev/Architect: the MOVEMENT seam (Site A, ``agents/subsystems/
movement.py`` via ``apply_world_patch``) has NO Session/orbital_content
reference today, so it cannot re-center the chart without new wiring. That gap
is logged as a blocking Delivery Finding — the "both seams" acceptance
criterion needs that decision before a Site-A wiring test can exist.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.session_room import SessionRoom
from sidequest.telemetry.setup import init_tracer

SECTOR_FIXTURE = (
    Path(__file__).resolve().parent.parent / "orbital" / "fixtures" / "world_sector_join"
)


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
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


def _sector_regions() -> dict[str, Region]:
    """Region ids match the sector fixture's star body-ids (identity join);
    ``ceron`` is the deliberately star-less region."""
    return {
        "yula": Region(
            name="Yula",
            summary="The amber system.",
            description="The amber system.",
            adjacent=["vorn", "ceron"],
        ),
        "vorn": Region(
            name="Vorn",
            summary="The blue system.",
            description="The blue system.",
            adjacent=["yula"],
        ),
        "ceron": Region(
            name="Ceron",
            summary="A charted region with no catalogued star.",
            description="A charted region with no catalogued star.",
            adjacent=["yula"],
        ),
    }


def _sector_pack() -> SimpleNamespace:
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(
            navigation_mode=NavigationMode.region,
            starting_region="yula",
            regions=_sector_regions(),
        )
    )
    return SimpleNamespace(worlds={"sector": world_obj})


def _character_named_rux():
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name="Rux",
        description="A belt-born pilot",
        personality="laconic",
        inventory=Inventory(),
    )
    return Character(
        core=core,
        char_class="Pilot",
        race="Human",
        backstory="Flew freight in the cloud before it paid worse than salvage.",
    )


def _orbital_bound_room(snap: GameSnapshot) -> SessionRoom:
    """A room whose Session has the sector orbital content loaded (unlike the
    ``room_for`` helper, which binds no ``world_dir``)."""
    room = SessionRoom(slug="sector_world", mode=GameMode.SOLO)
    room.bind_world(
        snapshot=snap,
        store=MagicMock(spec=SaveRepository),
        world_dir=SECTOR_FIXTURE,
    )
    return room


def test_region_relocation_recenters_orrery_and_emits_span(otel_capture) -> None:
    """A region-mode move from ``yula`` to ``vorn`` re-centers the party's
    orbital body and chart scope on star ``vorn`` and fires ``orbital.scope_bind``
    with trigger=relocation — driven through the real narration-apply seam."""
    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="sector",
        turn_manager=TurnManager(interaction=4),
    )
    snap.current_region = "yula"
    snap.pc_regions["Rux"] = "yula"
    snap.characters.append(_character_named_rux())

    room = _orbital_bound_room(snap)
    # Sanity: the room actually loaded the orbital tier (else the test would
    # vacuously "pass" against a None-content no-op).
    assert room.session.orbital_content is not None

    result = NarrationTurnResult(
        narration="Rux burns for the blue star and makes the Vorn approach.",
        location="Vorn — The Outer Anchorage",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=_sector_pack(),
        world="sector",
        player_name="Rux",
        room=room,
    )

    # The existing region advance still happened (the bind rides Site B).
    assert snap.pc_regions["Rux"] == "vorn"

    # New behavior: the orrery follows the party to the vorn system.
    assert room.session.party_body_id == "vorn"
    assert room.session.orbital_scope.center_body_id == "vorn"

    bind_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "orbital.scope_bind"
    ]
    assert len(bind_spans) == 1
    assert bind_spans[0].attributes["region_id"] == "vorn"
    assert bind_spans[0].attributes["body_id"] == "vorn"
    assert bind_spans[0].attributes["trigger"] == "relocation"
