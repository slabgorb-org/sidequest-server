"""Story 158-41 — Narrator max_turns must degrade loudly (ADR-006), not crash.

When the Anthropic SDK tool loop hits its turn cap it raises
``AnthropicSdkLoopExceeded`` from ``Orchestrator.run_narration_turn``
(``anthropic_sdk_client.py`` ~518). Today that exception escapes
``_execute_narration_turn`` uncaught — the method-level ``try:`` (wsh ~821)
has only a ``finally:`` — so it propagates out of the player-action handler,
forcing ``session.disconnect_save`` → room teardown → reconnect. The room is
wedged mid-turn.

Per ADR-006 (graceful degradation) the turn must degrade **loudly and
observably** and KEEP THE SESSION ALIVE:
  AC-1  the exhaustion does NOT propagate — the turn returns instead of crashing
        the room (no forced reconnect).
  AC-2  a player-facing degradation message is surfaced ("the engine could not
        resolve that, try rephrasing"-style), NOT the raw exception, and it does
        NOT demand a reconnect.
  AC-3  a watcher degrade event fires so the GM panel (lie-detector) can see the
        degraded path engaged — driven through the real turn, asserted via the
        watcher event, never a source-grep (CLAUDE.md "No Source-Text Wiring
        Tests" + OTEL Observability Principle).

These tests drive the REAL production method ``_execute_narration_turn`` (the
same entrypoint a live PLAYER_ACTION hits), so they double as the wiring test.

Contract names TEA pins for Dev (logged as deviations / delivery findings):
  * the watcher event MUST be published via the handler's ``_watcher_publish``
    channel (same channel as ``session.cost_ceiling_exceeded`` — its sibling
    terminal-SDK condition) with event type ``narrator.sdk_loop_exhausted``.
  * the player-facing text MUST contain "rephras" (the story's explicit
    guidance "try rephrasing").
Dev may rename either with a matching test update — they are contract anchors,
not implementation mandates.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import sidequest.server.websocket_session_handler as wsh
from sidequest.agents.anthropic_sdk_client import AnthropicSdkLoopExceeded
from sidequest.server.dispatch import monster_manual_inject
from tests.server.conftest import _build_turn_context_for_test

_DEGRADE_EVENT = "narrator.sdk_loop_exhausted"
_ACTION = "I bank hard and bring the cannons to bear on the corsair."


def _bypass_pre_narrator_seams(monkeypatch) -> None:
    """Skip the two pre-1075 LLM/IO seams so the turn reaches the orchestrator
    call deterministically and fails (only) at ``run_narration_turn``.

    ``monster_manual_inject.ensure_loaded`` would otherwise unpack the fixture
    pack's auto-mock ``effective_bestiary`` (the live develop red at
    ``monster_manual_inject.py:184``), and the intent-router pass would spawn a
    real Claude client. Both are explicitly documented module-level seams.
    """
    monkeypatch.setattr(monster_manual_inject, "ensure_loaded", lambda _sd: None)
    monkeypatch.setattr(
        wsh,
        "execute_intent_router_pre_narrator_pass",
        AsyncMock(return_value=(None, None)),
    )


def _arm_loop_exhaustion(sd, handler) -> None:
    """Make the narrator turn hit the SDK tool-loop cap, with a working validator."""
    sd.orchestrator.run_narration_turn = AsyncMock(
        side_effect=AnthropicSdkLoopExceeded(
            "agent-sdk tool loop did not converge — max_turns exhaustion "
            "raised at the transport boundary (max_turns=8)"
        )
    )
    handler._validator = MagicMock(submit=AsyncMock(), is_running=MagicMock(return_value=True))


def _serialize(messages: list[Any]) -> str:
    """Flatten outbound messages to a lowercased JSON string for text search."""
    dumped: list[Any] = []
    for m in messages:
        dump = getattr(m, "model_dump", None)
        if callable(dump):
            dumped.append(dump(mode="json"))
        else:
            dumped.append(repr(m))
    return json.dumps(dumped, default=str).lower()


def _any_reconnect_required(messages: list[Any]) -> bool:
    for m in messages:
        payload = getattr(m, "payload", None)
        if getattr(payload, "reconnect_required", False):
            return True
    return False


@pytest.mark.asyncio
async def test_sdk_loop_exhaustion_does_not_crash_the_turn(session_fixture, monkeypatch) -> None:
    """AC-1: the SDK tool-loop cap must NOT propagate out of the turn.

    Today ``AnthropicSdkLoopExceeded`` escapes and the upstream WS layer tears
    the room down (``disconnect_save`` → forced reconnect). The fix must catch
    it inside ``_execute_narration_turn`` and RETURN outbound messages — the
    propagation is the teardown trigger, so removing it is what keeps the
    session alive. RED today: the call raises.
    """
    sd, handler = session_fixture
    _bypass_pre_narrator_seams(monkeypatch)
    _arm_loop_exhaustion(sd, handler)

    tc = _build_turn_context_for_test(sd)
    # Must NOT raise — a raise here is the crash→teardown path.
    outbound = await handler._execute_narration_turn(sd, _ACTION, tc)

    assert outbound is not None, "degraded turn must return messages, not None"
    assert isinstance(outbound, list) and len(outbound) >= 1, (
        "degraded turn must surface at least one outbound message to the player"
    )


@pytest.mark.asyncio
async def test_sdk_loop_exhaustion_surfaces_player_rephrase_message_without_reconnect(
    session_fixture, monkeypatch
) -> None:
    """AC-2: a player-facing degrade message, NOT the raw exception, and no
    forced reconnect.

    The story mandates "the engine could not resolve that, try rephrasing".
    The message must reach the player (text contains "rephras") and must NOT
    set ``reconnect_required`` (the crash path forced a reconnect — the fix
    must not). The raw exception class must not leak into player-facing text.
    """
    sd, handler = session_fixture
    _bypass_pre_narrator_seams(monkeypatch)
    _arm_loop_exhaustion(sd, handler)

    tc = _build_turn_context_for_test(sd)
    outbound = await handler._execute_narration_turn(sd, _ACTION, tc)

    serialized = _serialize(outbound)
    assert "rephras" in serialized, (
        "player must be told to rephrase (story-mandated degrade guidance); "
        f"outbound was: {serialized[:400]}"
    )
    assert not _any_reconnect_required(outbound), (
        "degrade must keep the session alive — no message may demand a reconnect"
    )
    assert "anthropicsdkloopexceeded" not in serialized, (
        "the raw SDK exception must never be surfaced to the player"
    )


@pytest.mark.asyncio
async def test_sdk_loop_exhaustion_emits_watcher_degrade_event(
    session_fixture, monkeypatch
) -> None:
    """AC-3: the degrade decision must emit a watcher event so the GM panel
    (lie-detector) can see the SDK-loop exhaustion was handled.

    Per the OTEL Observability Principle every subsystem decision emits a
    watcher event. Captured by patching the handler's ``_watcher_publish``
    (the same channel ``session.cost_ceiling_exceeded`` uses). RED today: the
    exhaustion propagates before any degrade event is published.
    """
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info") -> None:
        captured.append({"event_type": event_type, "fields": fields, "component": component})

    sd, handler = session_fixture
    _bypass_pre_narrator_seams(monkeypatch)
    _arm_loop_exhaustion(sd, handler)
    monkeypatch.setattr(wsh, "_watcher_publish", _capture)

    tc = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, _ACTION, tc)

    degrade_events = [e for e in captured if e["event_type"] == _DEGRADE_EVENT]
    assert len(degrade_events) >= 1, (
        f"expected a {_DEGRADE_EVENT!r} watcher event on the SDK-loop degrade "
        f"path; captured event types were {[e['event_type'] for e in captured]}"
    )
    assert isinstance(degrade_events[0]["fields"], dict) and degrade_events[0]["fields"], (
        "the degrade event must carry a non-empty fields payload for the GM panel"
    )
