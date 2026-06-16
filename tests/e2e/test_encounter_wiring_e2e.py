"""End-to-end caverns_and_claudes combat walkthrough (story 3.4 closing gate).

No live LLM — orchestrator.run_narration_turn is scripted with AsyncMock.
Asserts the full pipeline: narrator -> encounter instantiation -> beat apply ->
OTEL spans -> CONFRONTATION dispatch -> threshold -> resolution.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.protocol.messages import ConfrontationMessage


@pytest.fixture
def span_exporter():
    """Attach an in-memory exporter to the running TracerProvider.

    Mirrors the ``otel_capture`` fixture pattern from test_room_graph_init.py:
    mounts an additional SimpleSpanProcessor alongside existing processors so
    handler span emissions fan out to in-memory for the duration of the test.
    Does NOT replace the global provider (which would corrupt other tests).
    """
    from sidequest.telemetry.setup import init_tracer

    init_tracer()  # idempotent — installs SDK provider if not yet set
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


@pytest.mark.asyncio
async def test_combat_walkthrough_initiate_tick_resolve(
    session_handler_factory,
    span_exporter,
):
    """Three-turn walkthrough: start combat, apply beat, cross threshold."""
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    run = AsyncMock()
    run.side_effect = [
        # Turn 1: narrator declares combat.
        NarrationTurnResult(
            narration="Goblins leap from the shadows!",
            confrontation="combat",
        ),
        # Turn 2: one attack beat (metric_delta=2 → momentum 0+2=2).
        NarrationTurnResult(
            narration="Steel rings on steel.",
            beat_selections=[
                BeatSelection(actor="Rux", beat_id="attack", target=None),
            ],
        ),
        # Turn 3: two shield_bash beats (metric_delta=4 each). 2+4=6 first,
        # then 6+4=10 crosses threshold_high=10 → resolve.
        NarrationTurnResult(
            narration="The last goblin falls.",
            beat_selections=[
                BeatSelection(actor="Rux", beat_id="shield_bash", target=None),
                BeatSelection(actor="Rux", beat_id="shield_bash", target=None),
            ],
        ),
    ]
    sd.orchestrator.run_narration_turn = run
    from sidequest.server.session_handler import _build_turn_context

    # --- Turn 1: combat begins ---
    msgs1 = await handler._execute_narration_turn(
        sd,
        "I attack!",
        _build_turn_context(sd),
    )
    assert sd.snapshot.encounter is not None
    assert sd.snapshot.encounter.encounter_type == "combat"
    assert not sd.snapshot.encounter.resolved
    assert sd.snapshot.encounter.metric.current == 0  # starting momentum

    c1 = [m for m in msgs1 if isinstance(m, ConfrontationMessage)]
    assert len(c1) == 1
    assert c1[0].payload.active is True
    assert c1[0].payload.type == "combat"

    names1 = {s.name for s in span_exporter.get_finished_spans()}
    assert "encounter.confrontation_initiated" in names1

    # --- Turn 2: attack beat ticks momentum ---
    msgs2 = await handler._execute_narration_turn(
        sd,
        "Press the attack!",
        _build_turn_context(sd),
    )
    assert sd.snapshot.encounter.metric.current == 2
    assert sd.snapshot.encounter.beat == 1
    assert not sd.snapshot.encounter.resolved

    c2 = [m for m in msgs2 if isinstance(m, ConfrontationMessage)]
    # live-to-live also emits a CONFRONTATION for UI repaint.
    assert len(c2) == 1
    assert c2[0].payload.active is True
    assert c2[0].payload.metric["current"] == 2

    names2 = {s.name for s in span_exporter.get_finished_spans()}
    assert "encounter.beat_applied" in names2
    assert "combat.tick" in names2

    # --- Turn 3: two shield_bash beats cross threshold_high ---
    msgs3 = await handler._execute_narration_turn(
        sd,
        "Finish them!",
        _build_turn_context(sd),
    )
    # 2 + 4 (first shield_bash) + 4 (second) = 10 → resolves.
    assert sd.snapshot.encounter.resolved is True
    assert sd.snapshot.encounter.structured_phase.value == "Resolution"
    # The break-on-resolve means the loop stops after the second beat —
    # beat counter moved from 1 → 2 → 3.
    assert sd.snapshot.encounter.beat == 3

    c3 = [m for m in msgs3 if isinstance(m, ConfrontationMessage)]
    assert len(c3) == 1
    assert c3[0].payload.active is False

    names3 = {s.name for s in span_exporter.get_finished_spans()}
    assert "encounter.resolved" in names3


@pytest.mark.asyncio
async def test_xp_award_higher_in_combat_than_out(
    session_handler_factory,
):
    """Regression (story 3.4 Task 13): in-combat turn awards 25 xp, 10 otherwise."""
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You take a quiet walk."),
    )
    from sidequest.server.session_handler import _build_turn_context

    # Out-of-combat turn.
    before = sd.snapshot.characters[0].core.xp
    await handler._execute_narration_turn(
        sd,
        "I walk.",
        _build_turn_context(sd),
    )
    after_out = sd.snapshot.characters[0].core.xp
    assert after_out - before == 10

    # Start combat, then take a beat turn in combat.
    sd.orchestrator.run_narration_turn = AsyncMock(
        side_effect=[
            NarrationTurnResult(narration="Goblins!", confrontation="combat"),
            NarrationTurnResult(
                narration="You strike.",
                beat_selections=[
                    BeatSelection(actor="Rux", beat_id="attack", target=None),
                ],
            ),
        ]
    )

    # Turn that creates the encounter (the XP check for this turn also sees
    # in_combat_now=True because the encounter is live post-apply).
    await handler._execute_narration_turn(
        sd,
        "I attack.",
        _build_turn_context(sd),
    )
    mid = sd.snapshot.characters[0].core.xp
    # Second combat turn: still live, attack beat ticks metric but does NOT
    # resolve (momentum 0+2=2 < 10). XP still 25.
    await handler._execute_narration_turn(
        sd,
        "Again!",
        _build_turn_context(sd),
    )
    after_combat = sd.snapshot.characters[0].core.xp
    assert after_combat - mid == 25
