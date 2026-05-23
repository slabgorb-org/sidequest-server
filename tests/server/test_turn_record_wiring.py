"""Wiring test: dispatch submits a TurnRecord to the validator.

Task 21 of the OTEL dashboard restoration plan. Verifies that
``_execute_narration_turn`` assembles a ``TurnRecord`` at the end of each
turn and delivers it to ``self._validator.submit``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.telemetry.turn_record import TurnRecord
from tests.server.conftest import _build_turn_context_for_test


@pytest.mark.asyncio
async def test_dispatch_submits_turn_record_to_validator(session_fixture) -> None:
    """dispatch must submit a TurnRecord at end of turn."""
    sd, handler = session_fixture

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="You look around. Nothing happens.",
            is_degraded=False,
            agent_duration_ms=1,
        )
    )

    mock_validator = MagicMock()
    mock_validator.submit = AsyncMock()
    mock_validator.is_running = MagicMock(return_value=True)
    handler._validator = mock_validator

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I look around.", turn_context)

    assert mock_validator.submit.await_count >= 1, (
        "dispatch must submit a TurnRecord at end of turn"
    )
    record = mock_validator.submit.await_args.args[0]
    assert isinstance(record, TurnRecord), f"expected TurnRecord, got {type(record)}"
    assert record.player_id == sd.player_id
    assert record.player_input == "I look around."


@pytest.mark.asyncio
async def test_turn_record_fields_populated(session_fixture) -> None:
    """The submitted TurnRecord carries turn_id, narration, and timestamps."""
    sd, handler = session_fixture

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="The torch flickers.",
            is_degraded=False,
            agent_duration_ms=50,
            token_count_in=100,
            token_count_out=60,
        )
    )

    mock_validator = MagicMock()
    mock_validator.submit = AsyncMock()
    mock_validator.is_running = MagicMock(return_value=True)
    handler._validator = mock_validator

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I examine the torch.", turn_context)

    assert mock_validator.submit.await_count >= 1
    record = mock_validator.submit.await_args.args[0]

    assert isinstance(record.turn_id, int)
    assert record.narration == "The torch flickers."
    assert record.agent_duration_ms == 50
    assert record.token_count_in == 100
    assert record.token_count_out == 60
    assert record.timestamp is not None
    assert record.snapshot_before_hash  # non-empty string
    assert record.snapshot_after is not None


@pytest.mark.asyncio
async def test_validator_none_does_not_raise(session_fixture) -> None:
    """When _validator is None, the turn completes without error."""
    sd, handler = session_fixture
    handler._validator = None

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="All is calm.",
            is_degraded=False,
            agent_duration_ms=1,
        )
    )

    turn_context = _build_turn_context_for_test(sd)
    # Should not raise.
    result = await handler._execute_narration_turn(sd, "I wait.", turn_context)
    assert result is not None
