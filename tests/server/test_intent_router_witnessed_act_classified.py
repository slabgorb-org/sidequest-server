"""Classification-result span after decompose (Plan 2b, Task 4)."""

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
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
)
from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass


class _FakePack:
    def __init__(self, witnessed_acts):
        self.witnessed_acts = witnessed_acts
        self.rules = None


class _StubRouter:
    """Records the state_summary it received and returns a fixed package."""

    def __init__(self, package: DispatchPackage):
        self._package = package
        self.seen_summary = None

    async def decompose(self, *, action, state_summary):
        self.seen_summary = state_summary
        return self._package


def _npc(name: str, *, location: str) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="A Munchkin villager.",
            personality="Hopeful.",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        belief_state=BeliefState(),
        location=location,
    )


def _oz_snapshot() -> GameSnapshot:
    snap = GameSnapshot(world_slug="oz")
    snap.genre_slug = "wry_whimsy"
    snap.player_seats = {"seat-1": "Dorothy"}
    snap.character_locations = {"Dorothy": "munchkin_country"}
    snap.npcs = [_npc("Boq", location="munchkin_country")]
    snap.political_state = PoliticalState(premises={}, blocs={}, ledger=[])
    return snap


def _acts():
    return [
        WitnessedActArchetype(id="expose_the_humbug", label="Expose the Humbug", description="x")
    ]


def _package_with_witnessed_act() -> DispatchPackage:
    return DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="Dorothy",
                raw_action="I pull the curtain aside in front of Boq",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="witnessed_act",
                        params={"act_id": "expose_the_humbug", "witnesses": ["Boq"]},
                        idempotency_key="wa-1",
                        visibility={"visible_to": "all"},
                        confidence=0.9,
                    )
                ],
            )
        ],
        cross_player=[],
        confidence_global=0.9,
    )


def _empty_package() -> DispatchPackage:
    return DispatchPackage(turn_id="t2", per_player=[], cross_player=[], confidence_global=0.5)


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


def _classified_spans(exporter):
    return [
        s
        for s in exporter.get_finished_spans()
        if s.name == "intent_router.witnessed_act_classified"
    ]


@pytest.mark.asyncio
async def test_classified_span_fires_with_emitted_count(otel_capture):
    snap = _oz_snapshot()
    router = _StubRouter(_package_with_witnessed_act())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=_FakePack(_acts()),
        action="I pull the curtain aside",
        player_name="Dorothy",
    )
    assert "witnessed_act_vocabulary" in router.seen_summary
    spans = _classified_spans(otel_capture)
    assert len(spans) == 1
    assert spans[0].attributes["emitted"] == 1
    assert spans[0].attributes["act_ids"] == "expose_the_humbug"
    assert spans[0].attributes["genre_slug"] == "wry_whimsy"


@pytest.mark.asyncio
async def test_classified_span_emitted_zero_when_router_declines(otel_capture):
    snap = _oz_snapshot()
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=_FakePack(_acts()),
        action="I admire the scenery",
        player_name="Dorothy",
    )
    spans = _classified_spans(otel_capture)
    assert len(spans) == 1
    assert spans[0].attributes["emitted"] == 0


@pytest.mark.asyncio
async def test_no_classified_span_in_non_political_world(otel_capture):
    snap = _oz_snapshot()
    snap.political_state = None
    router = _StubRouter(_empty_package())
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=_FakePack(_acts()),
        action="I admire the scenery",
        player_name="Dorothy",
    )
    assert _classified_spans(otel_capture) == []
