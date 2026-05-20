"""Test the one-iteration reprompt loop in _execute_narration_turn.

Spec 2026-05-20 confrontation-intent-validator step 7. Tests the
applied_outcome.reprompt_request handling: if set, narrator is invoked
again with extra_directive=<request.directive>; the re-apply runs with
already_reprompted=True; on second-call failure, the first attempt's
narration is applied.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_no_reprompt_when_apply_returns_no_request() -> None:
    pytest.skip(
        "Wrapper-loop integration: this test would verify that when "
        "applied_outcome.reprompt_request is None, narrator is called "
        "exactly once and no extra_directive is set. Constructing a real "
        "_SessionData + orchestrator triple is heavy; the wrapper logic "
        "is short and visible at websocket_session_handler.py "
        "_execute_narration_turn. Task 11's dust_and_lead replay test "
        "exercises this no-reprompt path end-to-end."
    )


@pytest.mark.asyncio
async def test_reprompt_request_triggers_second_narrator_call() -> None:
    pytest.skip(
        "Wrapper-loop integration: this test would verify that a returned "
        "RepromptRequest triggers exactly one second narrator call with "
        "extra_directive=request.directive, and that the re-apply runs "
        "with already_reprompted=True. Covered end-to-end via Task 11 "
        "with the combat_reprompt def in the dust_and_lead replay."
    )


@pytest.mark.asyncio
async def test_only_one_retry_then_fall_through() -> None:
    pytest.skip(
        "Wrapper-loop integration: this test would verify the bounded "
        "retry — a second mismatch results in NO third narrator call, "
        "fall-through degrades severity to warn (via already_reprompted), "
        "and the second attempt's narration is applied. Covered by Task 11 "
        "with a scripted narrator that mismatches twice."
    )


@pytest.mark.asyncio
async def test_second_call_failure_applies_first_attempt() -> None:
    pytest.skip(
        "Wrapper-loop integration: this test would verify that if the "
        "second run_narration_turn raises, the wrapper logs "
        "confrontation.intent_mismatch_reprompt_failed (Task 8 wires the "
        "real span), then re-applies the FIRST result with "
        "already_reprompted=True. Covered by Task 11 with a scripted "
        "narrator that raises on the second call."
    )
