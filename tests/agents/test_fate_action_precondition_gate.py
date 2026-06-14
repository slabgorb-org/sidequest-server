from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.dispatch_precondition_gate import (
    _fate_action_precondition_unmet,
    run_dispatch_precondition_gate,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)


def _pc(name: str) -> Character:
    return Character(
        core=CreatureCore(name=name, description="d", personality="p", fate_sheet=FateSheet()),
        char_class="Agent",
        race="Human",
        backstory="b",
    )


def _package() -> DispatchPackage:
    return DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="p1",
                raw_action="I shoot the thug",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="fate_action",
                        params={"action": "attack", "skill": "Fight", "target": "Thug"},
                        idempotency_key="fate_action_t1",
                        visibility=VisibilityTag(visible_to="all"),
                        confidence=0.95,
                    )
                ],
            )
        ],
        confidence_global=0.95,
    )


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_precondition_unmet_with_no_encounter():
    snap = GameSnapshot(genre_slug="pulp_noir", characters=[_pc("Vance")])
    assert _fate_action_precondition_unmet(snap) is not None


def test_precondition_met_with_active_conflict():
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[EncounterActor(name="Vance", role="lead", side="player")],
    )
    snap = GameSnapshot(genre_slug="pulp_noir", characters=[_pc("Vance")], encounter=enc)
    assert _fate_action_precondition_unmet(snap) is None


def test_gate_drops_fate_action_with_no_conflict_and_emits_span():
    snap = GameSnapshot(genre_slug="pulp_noir", characters=[_pc("Vance")])  # no encounter
    exporter, tracer = _otel()
    filtered = run_dispatch_precondition_gate(package=_package(), snapshot=snap, tracer=tracer)
    assert filtered.per_player[0].dispatch == []  # dropped
    assert "intent_router.dispatch.gated" in [s.name for s in exporter.get_finished_spans()]
