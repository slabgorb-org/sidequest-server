"""Hard-cap oversized-prompt canary on the synchronous narration path.

Pre-Story 61-3 this asserted the SOFT contract (log a warning + emit
``prompt_oversized`` + let the SDK call proceed). 61-3 promotes the
canary on BOTH paths to a HARD refuse: ``logger.error`` + emit
``prompt_oversized_hard`` (severity="error") + short-circuit the call,
returning a degraded ``NarrationTurnResult``. Same contract evolution
as the SDK path covered by ``test_61_3_hard_cap_oversized_canary.py``.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from sidequest.agents.claude_client import ClaudeResponse
from sidequest.agents.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_oversized_prompt_refuses_and_returns_degraded(simple_turn_context, caplog):
    """Force the budget below realistic prompt size; assert hard refuse."""
    client = AsyncMock()
    client.send_stateless = AsyncMock(
        return_value=ClaudeResponse(text='{"narration":"ok"}', session_id=None)
    )

    orch = Orchestrator(client=client)
    with (
        patch("sidequest.agents.orchestrator.PROMPT_BUDGET_BYTES_HARD", 10),
        caplog.at_level(logging.ERROR, logger="sidequest.agents.orchestrator"),
    ):
        result = await orch._run_narration_turn_synchronous("look", simple_turn_context)

    # Hard contract: the underlying client MUST NOT be called.
    client.send_stateless.assert_not_called()
    # Degraded shape so dispatch can surface the refusal.
    assert result.is_degraded is True
    assert result.narration  # player sees something, not an empty turn

    error_records = [
        r
        for r in caplog.records
        if "narrator.prompt_oversized" in r.getMessage() and r.levelno == logging.ERROR
    ]
    assert error_records, (
        f"oversized canary did not log at ERROR; caplog: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_normal_prompt_no_canary(simple_turn_context, caplog):
    """At normal size, no canary log fires (false-positive guard)."""
    client = AsyncMock()
    client.send_stateless = AsyncMock(
        return_value=ClaudeResponse(text='{"narration":"ok"}', session_id=None)
    )

    orch = Orchestrator(client=client)
    with caplog.at_level(logging.WARNING, logger="sidequest.agents.orchestrator"):
        await orch._run_narration_turn_synchronous("look", simple_turn_context)

    assert not any("narrator.prompt_oversized" in r.getMessage() for r in caplog.records)
