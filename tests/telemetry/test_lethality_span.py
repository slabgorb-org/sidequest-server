"""OTEL coverage for LethalityArbiter — Sebastien's GM panel reads these.

Matches the production-tracer-singleton pattern in tests/agents/conftest.py's
``otel_capture`` fixture: install an InMemorySpanExporter on the live
TracerProvider so we observe what production code actually emits.
"""

from __future__ import annotations

import pytest

from sidequest.agents.lethality_arbiter import LethalityArbiter
from sidequest.agents.subsystems import BankResult
from sidequest.game.creature_core import CreatureCore, EdgePool, Inventory
from sidequest.genre.models.lethality import LethalityPolicy, VerdictsOnZeroEdge
from sidequest.protocol.dispatch import DispatchPackage, PlayerDispatch
from sidequest.telemetry.spans import SPAN_INTENT_ROUTER_LETHALITY_ARBITRATE


@pytest.fixture
def otel_capture():
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

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


def _policy() -> LethalityPolicy:
    return LethalityPolicy(
        genre_key="heavy_metal",
        default_reversibility="permanent",
        verdicts_on_zero_edge=VerdictsOnZeroEdge(pc="dead", npc="dead"),
        soul_md_constraint="c",
        must_narrate="x",
        must_not_narrate="y",
    )


def _pc(current: int) -> CreatureCore:
    return CreatureCore(
        name="Alice",
        description="d",
        personality="p",
        inventory=Inventory(),
        edge=EdgePool(current=current, max=10, base_max=10),
    )


def test_arbitrate_emits_span_with_verdict_count(otel_capture):
    arbiter = LethalityArbiter(policy=_policy())
    pkg = DispatchPackage(
        turn_id="t42",
        per_player=[PlayerDispatch(player_id="alice", raw_action="x")],
        cross_player=[],
        confidence_global=1.0,
    )
    arbiter.arbitrate(
        package=pkg,
        bank_result=BankResult(),
        pc_cores_by_player={"alice": _pc(0)},
        npc_cores_by_name={},
    )

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_INTENT_ROUTER_LETHALITY_ARBITRATE
    ]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["turn_id"] == "t42"
    assert span.attributes["verdict_count"] == 1
    assert span.attributes["genre_key"] == "heavy_metal"


def test_arbitrate_no_verdict_still_emits_span(otel_capture):
    """Sebastien needs to see the arbiter ran even on quiet turns."""
    arbiter = LethalityArbiter(policy=_policy())
    pkg = DispatchPackage(
        turn_id="quiet-turn",
        per_player=[PlayerDispatch(player_id="alice", raw_action="x")],
        cross_player=[],
        confidence_global=1.0,
    )
    arbiter.arbitrate(
        package=pkg,
        bank_result=BankResult(),
        pc_cores_by_player={"alice": _pc(7)},
        npc_cores_by_name={},
    )
    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_INTENT_ROUTER_LETHALITY_ARBITRATE
    ]
    assert len(spans) == 1
    assert spans[0].attributes["verdict_count"] == 0
