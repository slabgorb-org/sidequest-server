"""Story 71-40 (RED) — narrator tool-loop iteration observability + cap (AC4).

The narrator's second LLM pass per turn runs ``AnthropicSdkClient.complete_with_tools``,
a loop that calls the model, dispatches requested tools, appends the results, and
calls again — up to ``max_iterations`` (default 8). A turn that keeps requesting
tools burns one full SDK round-trip per iteration and balloons solo-turn p95. The
loop already has a loud ceiling (``AnthropicSdkLoopExceeded`` at ``max_iterations``),
but there is **no per-turn observability on how many iterations a turn consumed**
and no cheaper-than-ceiling cap with a recorded cap-hit signal.

This story adds:

* a turn-level summary span ``narrator.tool_loop`` carrying ``iterations_used``
  (so a runaway turn is distinguishable from a one-shot turn — the GM panel
  lie-detector for "why is this turn slow");
* a configurable ``iteration_cap`` that, when crossed, fires a
  ``narrator.tool_loop.cap_hit`` span recording the cap and the iterations used
  — WITHOUT weakening the ``AnthropicSdkLoopExceeded`` fail-loud ceiling.

This is the REQUIRED narrator-side wiring test (context-story-71-40.md): it
drives the *real* ``complete_with_tools`` with a fake SDK transport and asserts
the new span emits through the live OTEL provider (the same pipeline the
WatcherSpanProcessor routes to the GM hub) — not a unit test of a counter in
isolation.

RED: the spans, the ``iteration_cap`` parameter, and the SPAN_ROUTES entries do
not exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sidequest.agents.anthropic_sdk_client import (
    AnthropicSdkClient,
    AnthropicSdkLoopExceeded,
)
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)

_SUMMARY_SPAN = "narrator.tool_loop"
_CAP_HIT_SPAN = "narrator.tool_loop.cap_hit"


# --- minimal SDK-shape fakes (mirror tests/agents/test_anthropic_sdk_client.py) ---


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: Any = None


@dataclass(frozen=True)
class _TextBlock:
    type: str
    text: str


@dataclass(frozen=True)
class _ToolUseBlockShape:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class _SdkResponse:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _FakeMessages:
    def __init__(self, responses: list[_SdkResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _SdkResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeMessages: out of scripted responses")
        return self._responses.pop(0)


class _FakeSdk:
    def __init__(self, responses: list[_SdkResponse]) -> None:
        self.messages = _FakeMessages(responses)


def _tool_use_response() -> _SdkResponse:
    return _SdkResponse(
        content=[_ToolUseBlockShape(type="tool_use", id="t", name="roll_dice", input={})],
        stop_reason="tool_use",
        usage=_Usage(input_tokens=10, output_tokens=1),
        model="claude-sonnet-4-6",
    )


def _text_response(text: str = "The scene resolves.") -> _SdkResponse:
    return _SdkResponse(
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=12, output_tokens=4),
        model="claude-sonnet-4-6",
    )


def _dispatch(block: ToolUseBlock) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)


_ROLL_TOOL = [ToolDefinition(name="roll_dice", description="r", input_schema={"type": "object"})]


def _spans_named(otel_capture, name: str) -> list[dict]:
    return [
        dict(s.attributes or {})
        for s in otel_capture.get_finished_spans()
        if s.name == name
    ]


# --- AC4 first half: iterations_used is observable per turn -------------------


@pytest.mark.asyncio
async def test_tool_loop_summary_span_records_iterations_used(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """A turn that requests a tool on iteration 1 then converges on iteration 2
    records ``iterations_used == 2`` on the ``narrator.tool_loop`` summary span.
    This is the per-turn count the GM panel needs to spot runaway loops."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeSdk(responses=[_tool_use_response(), _text_response()])
    client = AnthropicSdkClient(sdk=fake)

    result = await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll for it")],
        tools=_ROLL_TOOL,
        tool_dispatch=_dispatch,
        model="claude-sonnet-4-6",
    )

    assert result.stop_reason == "end_turn"
    summary = _spans_named(otel_capture, _SUMMARY_SPAN)
    assert len(summary) == 1, (
        f"exactly one {_SUMMARY_SPAN} span must fire per successful turn; got {len(summary)}"
    )
    assert summary[0].get("iterations_used") == 2, (
        "a tool-then-text turn made 2 SDK calls — iterations_used must be 2; "
        f"got {summary[0]}"
    )


@pytest.mark.asyncio
async def test_tool_loop_one_shot_turn_records_single_iteration(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """A one-shot turn (text on the first call, no tools) records
    ``iterations_used == 1`` — distinguishable from the runaway case above.
    Non-vacuous: proves the count is real, not hardcoded."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeSdk(responses=[_text_response("One and done.")])
    client = AnthropicSdkClient(sdk=fake)

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="look around")],
        tools=[],
        model="claude-sonnet-4-6",
    )

    summary = _spans_named(otel_capture, _SUMMARY_SPAN)
    assert len(summary) == 1
    assert summary[0].get("iterations_used") == 1, (
        f"a one-shot turn used a single iteration; got {summary[0]}"
    )


# --- AC4 second half: iteration cap + cap-hit span, ceiling preserved ---------


@pytest.mark.asyncio
async def test_iteration_cap_fires_cap_hit_span_and_ceiling_still_raises(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """AC4: with ``iteration_cap`` set BELOW the ``max_iterations`` ceiling and a
    model that requests a tool every iteration:

    (a) a ``narrator.tool_loop.cap_hit`` span fires recording the cap value and
        the iterations used (the GM panel surfaces the throttled turn), AND
    (b) the existing ``AnthropicSdkLoopExceeded`` fail-loud ceiling STILL raises
        (no silent swallow) — the cap adds observability, it does not replace
        the loud ceiling.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Always requests a tool — never converges.
    fake = _FakeSdk(responses=[_tool_use_response() for _ in range(10)])
    client = AnthropicSdkClient(sdk=fake)

    with pytest.raises(AnthropicSdkLoopExceeded):
        await client.complete_with_tools(
            system_blocks=[CacheableBlock(text="rules", cache=True)],
            messages=[Message(role="user", content="loop forever")],
            tools=_ROLL_TOOL,
            tool_dispatch=_dispatch,
            model="claude-sonnet-4-6",
            max_iterations=5,
            iteration_cap=2,
        )

    cap_hits = _spans_named(otel_capture, _CAP_HIT_SPAN)
    assert cap_hits, (
        f"crossing iteration_cap must fire a {_CAP_HIT_SPAN} span so the GM "
        "panel sees the throttled turn"
    )
    assert cap_hits[0].get("iteration_cap") == 2, (
        f"cap-hit span must record the cap value (2); got {cap_hits[0]}"
    )
    assert cap_hits[0].get("iterations_used", 0) >= 2, (
        "cap-hit span must record at least the cap's worth of iterations used; "
        f"got {cap_hits[0]}"
    )


@pytest.mark.asyncio
async def test_loop_ceiling_still_raises_without_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: the ``AnthropicSdkLoopExceeded`` ceiling is unchanged when no
    ``iteration_cap`` is supplied — a never-converging loop still raises loud at
    ``max_iterations``. The story must NOT weaken this fail-loud behavior."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeSdk(responses=[_tool_use_response() for _ in range(10)])
    client = AnthropicSdkClient(sdk=fake)

    with pytest.raises(AnthropicSdkLoopExceeded):
        await client.complete_with_tools(
            system_blocks=[CacheableBlock(text="rules", cache=True)],
            messages=[Message(role="user", content="loop forever")],
            tools=_ROLL_TOOL,
            tool_dispatch=_dispatch,
            model="claude-sonnet-4-6",
            max_iterations=3,
        )


@pytest.mark.asyncio
async def test_no_cap_hit_span_when_cap_not_reached(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """A turn that converges under the cap fires NO cap-hit span — the cap-hit
    signal is reserved for genuinely throttled turns, so it stays meaningful on
    the GM panel."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeSdk(responses=[_tool_use_response(), _text_response()])
    client = AnthropicSdkClient(sdk=fake)

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll for it")],
        tools=_ROLL_TOOL,
        tool_dispatch=_dispatch,
        model="claude-sonnet-4-6",
        max_iterations=8,
        iteration_cap=5,
    )

    assert not _spans_named(otel_capture, _CAP_HIT_SPAN), (
        "a turn that converged in 2 iterations (cap=5) must not fire a cap-hit span"
    )


# --- GM-panel routing wiring: the new spans are registered for the hub --------


def test_tool_loop_spans_registered_in_span_routes() -> None:
    """The new spans must be registered in ``SPAN_ROUTES`` so the
    WatcherSpanProcessor routes them to the live GM hub (the intent_router.py
    span-route pattern). Runtime-symbol interrogation — the documented
    legitimate exception to 'No Source-Text Wiring Tests' (we inspect the
    registry dict, not source text)."""
    from sidequest.telemetry.spans._core import SPAN_ROUTES

    for span_name in (_SUMMARY_SPAN, _CAP_HIT_SPAN):
        assert span_name in SPAN_ROUTES, (
            f"{span_name!r} must be registered in SPAN_ROUTES so it reaches the "
            f"GM panel via WatcherSpanProcessor; registered: {sorted(SPAN_ROUTES)}"
        )
