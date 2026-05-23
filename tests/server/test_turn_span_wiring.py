"""Wiring test: dispatching an action opens the turn root span.

Task 9 of the OTEL dashboard restoration plan. Verifies that
``_execute_narration_turn`` opens a ``turn`` root span and that any other
spans emitted during the dispatch appear as its children (no orphans).

This is the load-bearing invariant for the Timing tab — every trace must
be rooted in a ``turn`` span for the dashboard to group by turn.
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

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.telemetry.setup import init_tracer
from tests.server.conftest import _build_turn_context_for_test


@pytest.fixture
def otel_capture():
    """Install an in-memory exporter on the current TracerProvider.

    Calls ``init_tracer()`` (idempotent) to ensure the provider is set,
    then hooks a ``SimpleSpanProcessor`` into it so spans land in the
    in-memory exporter for assertion.

    Yields the exporter so the test can inspect finished spans.
    """
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
        exporter.clear()


@pytest.mark.asyncio
async def test_dispatch_opens_turn_span(otel_capture, session_fixture) -> None:
    """A turn dispatch produces at least one span named 'turn'.

    The ``turn`` span must be the root of the trace — every other span
    emitted during the dispatch should be a descendant of it, not an orphan.
    This is the load-bearing invariant for the Timing tab.
    """
    sd, handler = session_fixture

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="You look around. Nothing happens.",
            agent_duration_ms=1,
        )
    )

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I look around.", turn_context)

    spans = otel_capture.get_finished_spans()
    turn_spans = [s for s in spans if s.name == "turn"]
    assert turn_spans, (
        f"No 'turn' span opened during dispatch. Spans seen: {[s.name for s in spans]}"
    )

    # Only flag truly orphaned spans (parent is None) — grandchildren are fine.
    # ``snapshot.party_location_query`` (Wave 2B / story 45-48) is a getter
    # span emitted by ``GameSnapshot.party_location()`` that fires from many
    # code paths outside turn dispatch (chargen, projection rebuilds, REST
    # debug, turn-context construction before the turn span opens). It is
    # an intentional lie-detector hook for the GM panel and is exempt from
    # the "every span must descend from turn" rule.
    orphan_whitelist = {"snapshot.party_location_query"}
    orphans = [
        s for s in spans if s.name != "turn" and s.parent is None and s.name not in orphan_whitelist
    ]
    assert not orphans, f"Non-turn spans without any parent (orphaned): {[r.name for r in orphans]}"


@pytest.mark.asyncio
async def test_turn_span_carries_required_attributes(otel_capture, session_fixture) -> None:
    """The turn span carries turn_id, player_id, and agent_name attributes."""
    sd, handler = session_fixture

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="The torch flickers.",
            agent_duration_ms=1,
        )
    )

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I examine the torch.", turn_context)

    spans = otel_capture.get_finished_spans()
    turn_spans = [s for s in spans if s.name == "turn"]
    assert turn_spans

    t = turn_spans[0]
    attrs = t.attributes or {}
    assert "turn_id" in attrs, "turn span missing turn_id attribute"
    assert "player_id" in attrs, "turn span missing player_id attribute"
    assert "agent_name" in attrs, "turn span missing agent_name attribute"
    assert attrs["player_id"] == sd.player_id
    assert attrs["agent_name"] == "narrator"
