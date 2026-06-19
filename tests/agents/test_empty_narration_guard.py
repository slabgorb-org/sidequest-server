"""Empty player-facing narration must trip the degraded stall, never a clean success.

sq-playtest 2026-06-19 (BLOCKER-CRITICAL, Oz turns 14/15): a narrator turn whose
prose slot came back empty was persisted with ``content=''`` and
``is_degraded=False`` — a turn marked complete + clean with nothing to render. The
client hung on "narrator is thinking" with no recovery (resubmitting the same
phrasing reproduced the empty result). ``run_narration_turn`` must flag an empty
turn degraded, substitute the in-fiction stall so the surface is renderable, and
emit the ``narrator.empty_narration`` GM-panel lie-detector span.

Defect #1 (this guard): the player-facing hang. Defect #2 (WHY the prose came back
empty — tool-only response / prose in the wrong field) is separately tracked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sidequest.agents.claude_client import ClaudeResponse
from sidequest.agents.orchestrator import Orchestrator


def _client_returning(raw_text: str) -> AsyncMock:
    """AsyncMock LlmClient whose stateless call returns ``raw_text`` as the response.

    An ``AsyncMock`` is NOT a ``ToolingLlmClient``, so ``run_narration_turn`` routes
    through the synchronous path — the simplest way to drive a chosen raw response
    end-to-end through assembly + the empty-narration guard.
    """
    client = AsyncMock()
    client.send_stateless = AsyncMock(return_value=ClaudeResponse(text=raw_text, session_id=None))
    return client


@pytest.mark.asyncio
async def test_empty_narration_trips_degraded_stall(simple_turn_context):
    """Empty prose → is_degraded=True + renderable stall (was a blank clean success)."""
    orch = Orchestrator(client=_client_returning(""))

    result = await orch.run_narration_turn("I continue to use the oil can.", simple_turn_context)

    assert result.is_degraded is True, "empty narration must be flagged degraded, not a clean turn"
    assert result.narration.strip(), (
        "degraded turn must still carry renderable prose (no client hang)"
    )
    assert "world holds its breath" in result.narration.lower()


@pytest.mark.asyncio
async def test_whitespace_only_narration_also_trips(simple_turn_context):
    """A markup/whitespace-only residual that strips to empty trips the same guard.

    The reported turn logged raw len=20 that stripped to len=0 on persist — the
    guard keys on the *stripped* prose, not the raw length.
    """
    orch = Orchestrator(client=_client_returning("   \n  \t  \n"))

    result = await orch.run_narration_turn("look around", simple_turn_context)

    assert result.is_degraded is True
    assert result.narration.strip()


@pytest.mark.asyncio
async def test_empty_narration_emits_lie_detector_span(simple_turn_context, otel_capture):
    """An empty turn fires narrator.empty_narration so the GM panel can audit it."""
    orch = Orchestrator(client=_client_returning(""))

    await orch.run_narration_turn("I continue to use the oil can.", simple_turn_context)

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "narrator.empty_narration"]
    assert len(spans) == 1, "exactly one narrator.empty_narration span must fire on an empty turn"


@pytest.mark.asyncio
async def test_non_empty_narration_untouched(simple_turn_context, otel_capture):
    """Real prose passes through clean — no degrade, no stall, no lie-detector span."""
    orch = Orchestrator(client=_client_returning("The lamp sputters and the wick catches."))

    result = await orch.run_narration_turn("I continue to use the oil can.", simple_turn_context)

    assert result.is_degraded is False
    assert result.narration == "The lamp sputters and the wick catches."
    empty_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.empty_narration"
    ]
    assert not empty_spans, "a healthy turn must NOT fire the empty-narration lie detector"
