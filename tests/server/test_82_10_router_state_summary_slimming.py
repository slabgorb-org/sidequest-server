"""Story 82-10 — Intent Router state_summary slimming (ADR-110 amendment).

The router's ``_build_state_summary`` was Phase-A-only: a raw
``model_dump(exclude_defaults, exclude_none)`` shipping p50 ~35KB / p95
~54KB per classification call (measured on the 2026-06-06 router corpus,
295 real rows — ``npcs`` up to 82% of the payload, ``world_history`` 15KB
median where populated). These tests pin the extract-and-reuse cut:

* the shared ``apply_snapshot_slimming`` (Phase B drop + Phase C
  projections) now runs in the router builder;
* ``world_history`` gets the router-specific drop (narrator unaffected);
* split-party / unresolved location passes through LOUDLY (no gaslit-empty
  summary);
* the ``intent_router.state_summary_slimmed`` span carries the mandated
  before/after evidence;
* ``session_helpers`` re-exports keep the 61-2/61-5 import surface.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.history_chapter import HistoryChapter
from sidequest.game.session import GameSnapshot, NarrativeEntry, Npc
from sidequest.genre.loader import load_genre_pack
from sidequest.server.intent_router_pass import _build_state_summary

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"


@pytest.fixture
def test_pack():
    return load_genre_pack(_FIXTURE_PACK)


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


def _npc(name: str, *, last_seen_location: str | None = None) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="grizzled veteran of many caverns",
            personality="dour",
            inventory=Inventory(),
            hp=HpPool(current=5, max=5, base_max=5),
        ),
        last_seen_location=last_seen_location,
    )


def _snapshot_with_party_at(location: str) -> GameSnapshot:
    """Snapshot with one seated PC whose location resolves by consensus."""
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="mawdeep")
    snap.character_locations["Rux"] = location
    snap.player_seats["player:rux"] = "Rux"
    return snap


# ---------------------------------------------------------------------------
# AC-1: Phase B drop fields no longer ride into the router summary
# ---------------------------------------------------------------------------


def test_router_summary_drops_phase_b_fields() -> None:
    """``narrative_log`` (a Phase B field, unbounded by construction) is
    populated on the snapshot but absent from the router summary."""
    snap = _snapshot_with_party_at("ropefoot")
    snap.narrative_log.append(NarrativeEntry(author="narrator", content="The cavern mouth yawns."))

    summary = _build_state_summary(snap)

    assert "narrative_log" not in summary
    # The drop came from the shared registry, not an ad-hoc pop.
    from sidequest.server.snapshot_slimming import _PHASE_B_DROP_FIELDS

    assert "narrative_log" in _PHASE_B_DROP_FIELDS


# ---------------------------------------------------------------------------
# AC-2: Phase C — off-scene NPCs are projected out of the router summary
# ---------------------------------------------------------------------------


def test_router_summary_keeps_only_in_scene_npcs() -> None:
    """With a consensus party location, NPCs elsewhere are dropped from the
    summary while in-scene NPCs survive. The snapshot itself is NOT mutated
    — the projection is payload-side only."""
    snap = _snapshot_with_party_at("ropefoot")
    snap.npcs.append(_npc("Madge", last_seen_location="ropefoot"))
    snap.npcs.append(_npc("Farwander", last_seen_location="sunken_gate"))

    summary = _build_state_summary(snap)

    names = {
        (e.get("core") or {}).get("name") for e in summary.get("npcs", []) if isinstance(e, dict)
    }
    assert "Madge" in names
    assert "Farwander" not in names
    # Engine-side truth untouched: both NPCs still exist on the snapshot.
    assert {n.core.name for n in snap.npcs} == {"Madge", "Farwander"}


# ---------------------------------------------------------------------------
# AC-3: world_history — router-specific drop, narrator registry untouched
# ---------------------------------------------------------------------------


def test_router_summary_drops_world_history_but_registry_does_not() -> None:
    """``world_history`` is dropped from the ROUTER summary only. It must
    NOT be in the shared Phase B registry — the narrator keeps it as an
    anti-confabulation anchor (ADR-110 governance: bounded-by-convention)."""
    snap = _snapshot_with_party_at("ropefoot")
    snap.world_history.append(
        HistoryChapter(id="ch1", label="The Sundering", notes=["The deep cracked open."])
    )

    summary = _build_state_summary(snap)

    assert "world_history" not in summary
    from sidequest.server.snapshot_slimming import _PHASE_B_DROP_FIELDS

    assert "world_history" not in _PHASE_B_DROP_FIELDS


# ---------------------------------------------------------------------------
# AC-4: unresolved party location → loud pass-through, never a gaslit cut
# ---------------------------------------------------------------------------


def test_split_party_passes_npcs_through() -> None:
    """When seated PCs disagree on location (consensus → None), the npcs
    projection is SKIPPED — every NPC rides through rather than the summary
    gaslighting the router with an empty scene."""
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="mawdeep")
    snap.character_locations["Rux"] = "ropefoot"
    snap.character_locations["Stimpy"] = "sunken_gate"
    snap.player_seats["player:rux"] = "Rux"
    snap.player_seats["player:stimpy"] = "Stimpy"
    snap.npcs.append(_npc("Madge", last_seen_location="ropefoot"))
    snap.npcs.append(_npc("Farwander", last_seen_location="sunken_gate"))

    summary = _build_state_summary(snap)

    names = {
        (e.get("core") or {}).get("name") for e in summary.get("npcs", []) if isinstance(e, dict)
    }
    assert names == {"Madge", "Farwander"}


# ---------------------------------------------------------------------------
# AC-5: OTEL before/after evidence (the ADR-110 amendment's mandated span)
# ---------------------------------------------------------------------------


def test_slimmed_span_fires_with_before_after_evidence(otel_capture) -> None:
    """``intent_router.state_summary_slimmed`` fires once per build with
    bytes_before > bytes_after when the cut engaged, plus the projection
    counts the GM panel reads."""
    snap = _snapshot_with_party_at("ropefoot")
    snap.npcs.append(_npc("Madge", last_seen_location="ropefoot"))
    snap.npcs.append(_npc("Farwander", last_seen_location="sunken_gate"))
    snap.narrative_log.append(NarrativeEntry(author="narrator", content="x" * 2000))

    _build_state_summary(snap)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "intent_router.state_summary_slimmed"
    ]
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["bytes_before"] > attrs["bytes_after"]
    assert attrs["npcs_dropped"] == 1
    assert attrs["projection_skipped"] is False


def test_slimmed_span_records_projection_skipped_on_unresolved_location(
    otel_capture,
) -> None:
    """No seated party → consensus None → span records the degraded pass."""
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="mawdeep")

    _build_state_summary(snap)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "intent_router.state_summary_slimmed"
    ]
    assert len(spans) == 1
    assert spans[0].attributes["projection_skipped"] is True
    assert spans[0].attributes["npcs_dropped"] == 0


# ---------------------------------------------------------------------------
# AC-6: wiring — the production pass ships the slimmed summary to decompose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_narrator_pass_ships_slimmed_summary(test_pack) -> None:
    """End-to-end through ``execute_intent_router_pre_narrator_pass``: the
    state_summary handed to ``decompose`` has the Phase B/C + world_history
    cut applied AND retains the router-specific projections — proving the
    slimming is wired into the production call path, not just unit-tested."""
    from sidequest.protocol.dispatch import DispatchPackage
    from sidequest.server.intent_router_pass import (
        execute_intent_router_pre_narrator_pass,
    )

    snap = _snapshot_with_party_at("ropefoot")
    snap.genre_slug = "caverns_and_claudes"
    snap.narrative_log.append(NarrativeEntry(author="narrator", content="Deep echoes."))
    snap.world_history.append(HistoryChapter(id="ch1", label="The Sundering"))
    snap.npcs.append(_npc("Madge", last_seen_location="ropefoot"))
    snap.npcs.append(_npc("Farwander", last_seen_location="sunken_gate"))

    empty_package = DispatchPackage(
        turn_id="test-turn",
        per_player=[],
        cross_player=[],
        confidence_global=0.8,
    )
    mock_router = AsyncMock()
    mock_router.decompose = AsyncMock(return_value=empty_package)

    await execute_intent_router_pre_narrator_pass(
        intent_router=mock_router,
        snapshot=snap,
        pack=test_pack,
        action="I look around",
        player_name="Rux",
    )

    mock_router.decompose.assert_called_once()
    shipped = mock_router.decompose.call_args[1]["state_summary"]
    assert "narrative_log" not in shipped
    assert "world_history" not in shipped
    npc_names = {
        (e.get("core") or {}).get("name") for e in shipped.get("npcs", []) if isinstance(e, dict)
    }
    assert npc_names == {"Madge"}
    # Router-specific additions still layer AFTER the cut.
    assert "confrontation_types" in shipped


# ---------------------------------------------------------------------------
# AC-7: session_helpers re-export surface (61-2 / 61-5 contracts) unchanged
# ---------------------------------------------------------------------------


def test_session_helpers_reexports_are_the_shared_objects() -> None:
    """The narrator module re-exports the slimming registry/helpers as the
    SAME objects — two consumers, one audited cut, no drift possible."""
    from sidequest.server import session_helpers, snapshot_slimming

    assert session_helpers._PHASE_B_DROP_FIELDS is snapshot_slimming._PHASE_B_DROP_FIELDS
    assert (
        session_helpers._apply_phase_c_projections is snapshot_slimming._apply_phase_c_projections
    )
    assert session_helpers._KNOWN_FACTS_TAIL_K == snapshot_slimming._KNOWN_FACTS_TAIL_K
    assert session_helpers._DISCOVERED_CLUES_CAP == snapshot_slimming._DISCOVERED_CLUES_CAP
    assert session_helpers.apply_snapshot_slimming is snapshot_slimming.apply_snapshot_slimming
