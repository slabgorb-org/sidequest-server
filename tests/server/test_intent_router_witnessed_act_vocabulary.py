"""witnessed_act state-summary projections + present-NPC helper (Plan 2b, Tasks 2-3)."""

from __future__ import annotations

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.belief_state import BeliefState
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.political_state import PoliticalState
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.models.premises import WitnessedActArchetype
from sidequest.server.intent_router_pass import _build_state_summary, _present_npc_names


def _npc(name: str, *, location: str | None = None) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="A villager of the Munchkin country.",
            personality="Wary but hopeful.",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        belief_state=BeliefState(),
        location=location,
    )


def _oz_snapshot() -> GameSnapshot:
    """A hydrated political snapshot whose party has a consensus location."""
    snap = GameSnapshot(world_slug="oz")
    snap.genre_slug = "wry_whimsy"
    snap.player_seats = {"seat-1": "Dorothy"}
    snap.character_locations = {"Dorothy": "munchkin_country"}
    snap.npcs = [
        _npc("Boq", location="munchkin_country"),     # present
        _npc("Glinda", location="quadling_country"),  # elsewhere
    ]
    snap.political_state = PoliticalState(
        premises={}, blocs={}, ledger=[]
    )
    return snap


def _acts() -> list[WitnessedActArchetype]:
    return [
        WitnessedActArchetype(id="expose_the_humbug", label="Expose the Humbug", description="Pull the curtain."),
        WitnessedActArchetype(id="refuse_the_premise", label="Refuse the Premise", description="Decline the rule."),
    ]


def test_present_npc_names_returns_only_in_scene_npcs():
    snap = _oz_snapshot()
    names = _present_npc_names(snap)
    assert names == ["Boq"]  # Glinda is in quadling_country, not present


def test_present_npc_names_empty_when_party_location_unresolved():
    # No seated PCs → party_location() is None → no one is "present".
    snap = _oz_snapshot()
    snap.player_seats = {}
    assert _present_npc_names(snap) == []


class _FakePack:
    """Minimal duck-typed pack: only the fields _build_state_summary reads."""

    def __init__(self, witnessed_acts):
        self.witnessed_acts = witnessed_acts
        self.rules = None  # no confrontations → confrontation block is skipped


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


def test_state_summary_includes_vocabulary_and_present_npcs_in_political_world():
    snap = _oz_snapshot()
    summary = _build_state_summary(snap, pack=_FakePack(_acts()))

    assert "witnessed_act_vocabulary" in summary
    vocab = summary["witnessed_act_vocabulary"]
    assert {v["id"] for v in vocab} == {"expose_the_humbug", "refuse_the_premise"}
    assert set(vocab[0].keys()) == {"id", "label", "description"}
    assert summary["present_npcs"] == ["Boq"]


def test_state_summary_omits_both_when_no_political_state():
    snap = _oz_snapshot()
    snap.political_state = None  # world ships no premise/bloc layer
    summary = _build_state_summary(snap, pack=_FakePack(_acts()))
    assert "witnessed_act_vocabulary" not in summary
    assert "present_npcs" not in summary


def test_state_summary_omits_both_when_pack_has_no_acts():
    snap = _oz_snapshot()
    summary = _build_state_summary(snap, pack=_FakePack([]))
    assert "witnessed_act_vocabulary" not in summary
    assert "present_npcs" not in summary


def test_state_summary_omits_both_without_pack():
    snap = _oz_snapshot()
    summary = _build_state_summary(snap)  # pack=None
    assert "witnessed_act_vocabulary" not in summary
    assert "present_npcs" not in summary


def test_vocabulary_injection_emits_span(otel_capture):
    snap = _oz_snapshot()
    _build_state_summary(snap, pack=_FakePack(_acts()))
    spans = [
        s for s in otel_capture.get_finished_spans()
        if s.name == "intent_router.witnessed_act_vocabulary"
    ]
    assert len(spans) == 1
    assert spans[0].attributes["act_count"] == 2
    assert spans[0].attributes["present_npc_count"] == 1
    assert spans[0].attributes["genre_slug"] == "wry_whimsy"


def test_no_vocabulary_span_in_non_political_world(otel_capture):
    snap = _oz_snapshot()
    snap.political_state = None
    _build_state_summary(snap, pack=_FakePack(_acts()))
    spans = [
        s for s in otel_capture.get_finished_spans()
        if s.name == "intent_router.witnessed_act_vocabulary"
    ]
    assert len(spans) == 0
