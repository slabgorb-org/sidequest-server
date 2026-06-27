"""Story 158-41 — the narrator max_turns ceiling must degrade LOUDLY, not crash+teardown.

GENERAL narrator robustness (split out of 158-29; NOT dogfight-specific). The Anthropic
agent-SDK tool loop raises ``AnthropicSdkLoopExceeded`` when it cannot converge within
``max_turns`` — at the transport boundary (anthropic_sdk_client.py ~line 518) OR as the
terminal ``error_max_turns`` ResultMessage (~line 676). Both raise the SAME typed
exception, and today it propagates RAW out of ``Orchestrator.run_narration_turn``
(called from websocket_session_handler.py:1075). The handler's turn body has no catch,
so the exception escapes → ``session.disconnect_save`` → room teardown → forced
reconnect. The whole table is kicked because one turn could not resolve.

Per ADR-006 (graceful degradation) and SOUL "the room doesn't pause": when the narrator
loop fails to converge the turn must degrade **loudly and observably** while KEEPING THE
SESSION ALIVE:

  1. catch the typed ``AnthropicSdkLoopExceeded`` at the orchestrator seam (NOT a broad
     ``except Exception`` — a parser bug must still propagate raw; No Silent Fallbacks /
     lang-review #1),
  2. emit an OTEL span + a GM-panel watcher event so the operator sees the engine bailed
     (CLAUDE.md OTEL Observability Principle — the lie detector must show the degrade),
  3. surface a player-facing "the engine could not resolve that — try rephrasing"
     narration via the existing ``_degraded_result`` contract (``is_degraded=True``), and
  4. RETURN that result rather than raise, so the handler's existing degraded-result path
     renders a NARRATION card and the room continues — no teardown.

Because the fix returns the same ``_degraded_result`` shape the oversized-canary
(Story 61-3) and empty-prose-stall (Story 153-11) paths already return, the websocket
handler needs NO new code: it already logs ``degraded=%s`` and renders the degraded
narration (websocket_session_handler.py:1077-1083). The wiring proof here is therefore
at the orchestrator's real entry point (``run_narration_turn``, the method the handler
calls): a turn that would have crashed now returns a degraded result AND lights the
GM-panel hub.

The *client* still RAISES ``AnthropicSdkLoopExceeded`` (Story 119-4's fail-loud
convergence contract is unchanged — see test_119_4_auth_durability_cost_otel.py). This
story adds the *orchestrator-side* catch that turns that raise into a survivable degrade.

Contract names asserted below — the Dev implements code that fires these:
  span:  narrator.max_turns_degraded   (attrs: turn_number)
  event: narrator_max_turns_degraded   (component=orchestrator, severity=error)
  player narration: case-insensitively contains "rephrase"; DISTINCT from the
    empty-prose stall ("The world holds its breath.") and the budget-refuse
    ("[narrator-overload — operator paged]") so session-recording grep can tell the
    three degrade causes apart (the 61-3 distinguishability doctrine).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Importing the tools package wires the 26 adapters onto default_registry, matching the
# production SDK path's expectations (same as test_153_11_empty_prose_upstream).
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkLoopExceeded
from sidequest.agents.orchestrator import NarrationTurnResult, Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub
from tests._helpers.doubles import FakeSocket
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

# --- the contract under test (Dev implements code that fires these) ---------
DEGRADE_SPAN = "narrator.max_turns_degraded"
DEGRADE_EVENT = "narrator_max_turns_degraded"
# Sibling degrade narrations the max_turns degrade MUST stay distinct from.
EMPTY_PROSE_STALL = "The world holds its breath."
BUDGET_REFUSE = "[narrator-overload — operator paged]"

_DRIVE_TURN = 7


class _RaisingClient(FakeAnthropicSdkClient):
    """A ToolingLlmClient whose ``complete_with_tools`` raises before returning.

    Subclasses the shared fake so ``isinstance(client, ToolingLlmClient)`` still holds
    and ``run_narration_turn`` routes to ``_run_narration_turn_sdk`` (the path under
    test). The scripted-response machinery is unused — the call raises immediately,
    exactly as the real client does when the agent-SDK tool loop hits its cap.
    """

    def __init__(self, exc: Exception) -> None:
        super().__init__(responses=[])
        self._exc = exc

    async def complete_with_tools(self, *args: object, **kwargs: object) -> Any:  # type: ignore[override]
        raise self._exc


class _SentinelClientError(RuntimeError):
    """A NON-loop-exceeded failure (stands in for a parser/transport bug). Used to pin
    that the graceful-degrade catch is type-specific and never a blanket swallow."""


class _FakeRegistry:
    """Minimal PromptRegistry stand-in (mirrors test_153_11_empty_prose_upstream)."""

    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        return []


def _response(*, text: str, stop_reason: str) -> ScriptedResponse:
    return ScriptedResponse(
        text=text,
        stop_reason=stop_reason,
        input_tokens=200,
        output_tokens=12,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
        tool_uses=[],
    )


async def _drive_sdk_turn(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeAnthropicSdkClient,
    *,
    action: str = "I bank hard and try to get on the freighter's tail.",
    turn_number: int = _DRIVE_TURN,
) -> NarrationTurnResult:
    """Run ``action`` through the REAL ``run_narration_turn`` SDK path with ``client``.

    This is the exact orchestrator entry point websocket_session_handler.py:1075 calls,
    so a degrade observed here is reachable from the live turn path (wiring, not a
    private-method unit test).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    orch = Orchestrator(client=client)

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(character_name="Kael", genre="space_opera", turn_number=turn_number)
    return await orch.run_narration_turn(action, ctx)


def _degrade_spans(otel_capture: InMemorySpanExporter) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == DEGRADE_SPAN]


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the watcher hub to the test loop and clear subscribers (test_61_3 pattern)."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


# ---------------------------------------------------------------------------
# AC1 — a max_turns loop-exceeded no longer crashes the turn; it returns a
#        degraded result. (Today: run_narration_turn re-raises → teardown.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_exceeded_returns_degraded_not_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SDK loop raises ``AnthropicSdkLoopExceeded``, ``run_narration_turn`` must
    swallow that SPECIFIC type into a degraded result instead of propagating — the
    propagation is what tears the room down today.
    """
    client = _RaisingClient(
        AnthropicSdkLoopExceeded(
            "agent-sdk tool loop did not converge "
            "(subtype=error_max_turns, num_turns=8, max_turns=8)"
        )
    )

    # Must NOT raise. If this line raises, the room would have been torn down.
    result = await _drive_sdk_turn(monkeypatch, client)

    assert result.is_degraded is True, (
        "a max_turns loop-exceeded must produce a degraded NarrationTurnResult so the "
        "handler renders a card and keeps the room alive (ADR-006), not raise to teardown"
    )
    assert result.narration.strip(), (
        "the player must see something — a degraded turn still renders prose, never a "
        "blank/empty turn the client hangs on"
    )


# ---------------------------------------------------------------------------
# AC2 — the player-facing message is actionable "try rephrasing" guidance, and
#        DISTINCT from the other two degrade narrations.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_exceeded_player_message_is_rephrase_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degrade narration must tell the player to rephrase (the story's required
    surface) and must NOT collide with the empty-prose stall or the budget-refuse line —
    session-recording grep has to tell the three degrade causes apart (61-3 doctrine).
    """
    client = _RaisingClient(AnthropicSdkLoopExceeded("max_turns exhaustion"))

    result = await _drive_sdk_turn(monkeypatch, client)

    assert "rephrase" in result.narration.lower(), (
        "the max_turns degrade must surface the actionable 'try rephrasing' guidance the "
        f"story requires; got {result.narration!r}"
    )
    assert result.narration.strip() != EMPTY_PROSE_STALL, (
        "the max_turns degrade must be DISTINCT from the empty-prose stall so grep can "
        "tell a non-convergence from an empty-prose turn"
    )
    assert BUDGET_REFUSE not in result.narration, (
        "the max_turns degrade must be DISTINCT from the budget-refuse line (61-3 "
        "distinguishability)"
    )


# ---------------------------------------------------------------------------
# AC3 — the degrade emits an OTEL span (observability; the GM-panel lie detector
#        must see the engine bailed, not infer it from silence).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_exceeded_emits_degrade_span(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """Exactly one ``narrator.max_turns_degraded`` span fires, carrying the turn number
    for GM-panel correlation. Per CLAUDE.md, a subsystem decision that isn't on a span is
    invisible to the lie detector — the degrade is a decision and must be on one.
    """
    client = _RaisingClient(AnthropicSdkLoopExceeded("max_turns exhaustion"))

    await _drive_sdk_turn(monkeypatch, client)

    spans = _degrade_spans(otel_capture)
    assert len(spans) == 1, (
        f"a max_turns degrade must fire exactly one {DEGRADE_SPAN} span; got {len(spans)} "
        f"(all: {[s.name for s in otel_capture.get_finished_spans()]})"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("turn_number") == _DRIVE_TURN, (
        "the degrade span must carry the turn number so the GM panel can correlate it to "
        f"the failed turn; got {attrs.get('turn_number')!r}"
    )


# ---------------------------------------------------------------------------
# AC4 — WIRING: the degrade condition reaches a LIVE GM-panel watcher subscriber,
#        driven through the real run_narration_turn (not just logged in isolation).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_exceeded_emits_watcher_event_to_gm_panel(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """Drive the REAL ``run_narration_turn`` SDK path, subscribe a socket to the live
    hub, and prove the ``narrator_max_turns_degraded`` GM-panel event arrives — the
    CLAUDE.md "every test suite needs a wiring test" gate. severity=error because the
    narrator engine could not resolve the turn at all (a server-side fault, lang-review
    #4), and component=orchestrator because that is where the catch lives.
    """
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    client = _RaisingClient(AnthropicSdkLoopExceeded("max_turns exhaustion"))
    await _drive_sdk_turn(monkeypatch, client)
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == DEGRADE_EVENT]
    assert len(events) == 1, (
        f"exactly one {DEGRADE_EVENT} event must reach watcher subscribers; got "
        f"{len(events)} (all: {[e.get('event_type') for e in sock.events]})"
    )
    event = events[0]
    assert event.get("severity") == "error", (
        "a turn the narrator could not resolve is a server-side fault → severity 'error'; "
        f"got {event.get('severity')!r}"
    )
    assert event.get("component") == "orchestrator", (
        f"the max_turns catch lives in the orchestrator; got {event.get('component')!r}"
    )
    assert event.get("fields", {}).get("turn_number") == _DRIVE_TURN, (
        f"the GM panel needs the turn number to correlate; got fields={event.get('fields')!r}"
    )


# ---------------------------------------------------------------------------
# Negative guard (No Silent Fallbacks / lang-review #1) — the catch is
# type-specific. A non-loop-exceeded failure must NOT be masked as a
# "try rephrasing" degrade; it propagates raw so a real bug stays loud.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_loop_exceeded_exception_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """A parser/transport bug (here a sentinel ``RuntimeError``) must NOT be coerced into
    the max_turns degrade — that would mask an unknown fault behind "try rephrasing" and
    light the GM panel with the wrong cause. The catch must key on
    ``AnthropicSdkLoopExceeded`` ONLY (never ``except Exception``): the sentinel
    propagates and the degrade span never fires.
    """
    client = _RaisingClient(_SentinelClientError("apply_status parsed an empty content list"))

    with pytest.raises(_SentinelClientError):
        await _drive_sdk_turn(monkeypatch, client)

    assert _degrade_spans(otel_capture) == [], (
        f"a non-loop-exceeded fault must NOT fire the {DEGRADE_SPAN} span — the catch is "
        "type-specific, not a blanket Exception swallow"
    )


# ---------------------------------------------------------------------------
# False-positive guard — a healthy SDK turn fires NEITHER the degrade span nor
# the event, and is not degraded. Guards against a catch that triggers on every
# turn or a span that always fires.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_sdk_turn_emits_no_degrade_signal(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub, otel_capture: InMemorySpanExporter
) -> None:
    """Real prose passes through clean: no degrade span, no watcher event, not degraded."""
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    client = FakeAnthropicSdkClient(
        responses=[_response(text="The freighter's engines flare as you close in.", stop_reason="end_turn")]
    )
    result = await _drive_sdk_turn(monkeypatch, client)
    await asyncio.sleep(0.05)

    assert _degrade_spans(otel_capture) == [], (
        "a healthy turn must NOT fire the max_turns degrade span"
    )
    assert [e for e in sock.events if e.get("event_type") == DEGRADE_EVENT] == [], (
        "a healthy turn must NOT publish the max_turns degrade watcher event"
    )
    assert result.is_degraded is False
    assert result.narration == "The freighter's engines flare as you close in."
