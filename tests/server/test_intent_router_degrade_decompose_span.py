"""Story 71-29: the intent_router degrade path emits a decompose span.

The happy path (``IntentRouter.decompose`` success) emits an
``intent_router.decompose`` span. That span name is registered in ``SPAN_ROUTES``
as a ``state_transition`` event, so ``WatcherSpanProcessor.on_end`` routes it to
the live GM dashboard via ``hub.publish`` — giving the GM panel an event for
every turn the spine ran. (This is a live-dashboard broadcast, NOT a durable
``turn_telemetry`` write: span routing calls ``hub.publish``, never
``publish_event`` — the happy path does not write a turn_telemetry row either.)

The degrade path is the operator opt-in (``SIDEQUEST_INTENT_ROUTER_DEGRADE_ON_FAIL``):
``decompose`` raises ``IntentRouterFailure`` BEFORE reaching its own decompose
span, the handler catches it, and the turn continues with
``dispatch_package=None``. Before this fix that branch emitted NO
``intent_router.decompose`` span, so the GM panel was blind on degraded turns —
it could not tell a degrade from a turn where the spine never executed at all.

These tests drive the REAL handler degrade branch (via ``_execute_narration_turn``)
and assert the decompose span fires with ``degraded=True`` / ``dispatch_count=0``.
Because the span name ``intent_router.decompose`` is registered in ``SPAN_ROUTES``
exactly like the happy-path span, firing it here reaches the GM dashboard through
the same routing mechanism — no reimplementation.

Non-vacuous: with the fix reverted (the degrade branch emits no span), the
``intent_router.decompose`` lookup returns an empty list and both assertions
fail. The happy-path regression test (in ``tests/agents/test_intent_router.py``)
proves the marker is ``False`` on a successful turn, so the assertion is not
trivially satisfied by any decompose span.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sidequest.agents.intent_router import IntentRouterFailure
from sidequest.agents.orchestrator import NarrationTurnResult
from tests.server.conftest import _build_turn_context_for_test, span_attrs_by_name

_DECOMPOSE_SPAN = "intent_router.decompose"


def _stub_narration(handler) -> None:
    """Give the orchestrator a canned narration result and silence the
    validator so the turn completes without external dependencies."""
    from unittest.mock import MagicMock

    handler._session_data.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="You press on, unaided by the spine.",
            is_degraded=False,
            agent_duration_ms=1,
        )
    )
    mock_validator = MagicMock()
    mock_validator.submit = AsyncMock()
    mock_validator.is_running = MagicMock(return_value=True)
    handler._validator = mock_validator


@pytest.mark.asyncio
async def test_degrade_path_emits_decompose_span_with_degraded_marker(
    session_fixture, otel_exporter, monkeypatch
) -> None:
    """When the router fails and the operator opt-in degrade env is set, the
    turn still emits an ``intent_router.decompose`` span (degraded=True,
    dispatch_count=0). This is the span that routes to the live GM dashboard
    (via WatcherSpanProcessor → hub.publish); firing it on the degrade path is
    what un-blinds the GM panel."""
    sd, handler = session_fixture
    _stub_narration(handler)

    # Operator opt-in: the degrade branch is gated on this env var. Without it
    # the handler re-raises (the fail-loud default), so set it to reach the
    # branch under test.
    monkeypatch.setenv("SIDEQUEST_INTENT_ROUTER_DEGRADE_ON_FAIL", "1")

    # Drive the REAL degrade branch: the pre-narrator pass raises
    # IntentRouterFailure (after the router's bounded retry, mirrored here as a
    # single raise) so the handler's except branch runs.
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.execute_intent_router_pre_narrator_pass",
        AsyncMock(side_effect=IntentRouterFailure("synthetic: router down after retry")),
    )

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I look around.", turn_context)

    decompose_attrs = span_attrs_by_name(otel_exporter, _DECOMPOSE_SPAN)
    assert decompose_attrs, (
        "degrade path emitted NO intent_router.decompose span — the GM panel "
        "would be blind on a degraded turn (Story 71-29 regression)."
    )
    degraded_spans = [a for a in decompose_attrs if a.get("degraded") is True]
    assert degraded_spans, (
        "a decompose span fired but none carried degraded=True; the degrade "
        f"branch must mark its span. Got attrs: {decompose_attrs}"
    )
    assert degraded_spans[0].get("dispatch_count") == 0, (
        "a degraded turn dispatched no engines — dispatch_count must be 0. "
        f"Got: {degraded_spans[0]}"
    )


@pytest.mark.asyncio
async def test_degrade_branch_not_taken_without_env_reraises(
    session_fixture, otel_exporter, monkeypatch
) -> None:
    """Fail-loud default (no env): IntentRouterFailure propagates out of the
    turn and NO degraded decompose span is emitted. Guards that the new span
    is scoped strictly to the operator opt-in, not the default path."""
    sd, handler = session_fixture
    _stub_narration(handler)

    monkeypatch.delenv("SIDEQUEST_INTENT_ROUTER_DEGRADE_ON_FAIL", raising=False)
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.execute_intent_router_pre_narrator_pass",
        AsyncMock(side_effect=IntentRouterFailure("synthetic: router down after retry")),
    )

    turn_context = _build_turn_context_for_test(sd)
    with pytest.raises(IntentRouterFailure):
        await handler._execute_narration_turn(sd, "I look around.", turn_context)

    degraded = [
        a
        for a in span_attrs_by_name(otel_exporter, _DECOMPOSE_SPAN)
        if a.get("degraded") is True
    ]
    assert not degraded, (
        "fail-loud default must not emit a degraded decompose span — the "
        "degrade marker is reserved for the operator opt-in branch."
    )
