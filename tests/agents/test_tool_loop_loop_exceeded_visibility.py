"""Story 82-9 (RED) — loop-exceeded turns are no longer invisible (AC2 + AC5).

71-40 fires the ``narrator.tool_loop`` summary span ONLY on the converged path:
a turn that exhausts ``max_iterations`` and raises ``AnthropicSdkLoopExceeded``
emits NO summary span (Reviewer Gap finding). Those are the worst-latency turns —
exactly the ones the AC5 diagnosis most wants ``iterations_used`` for — and they
are blind to the metric.

This story emits the summary span on the raise path too, carrying a
``loop_exceeded`` marker so the GM panel can tell a converged-but-deep turn from a
ceiling-blown one, WITHOUT weakening the fail-loud ``AnthropicSdkLoopExceeded``
ceiling. This SUPERSEDES the old 71-40 "summary-absent-on-raise" gap: the summary
is now PRESENT on raise (with ``loop_exceeded=True``).

Also hardens AC5's "exactly-once cap-hit" gap: across many post-cap iterations the
cap-hit span fires exactly once, not per iteration.

Drives the REAL ``complete_with_tools`` with a fake SDK transport (the
71-40 test pattern) and asserts on spans through the live OTEL provider.

RED: the raise path emits no summary span and carries no ``loop_exceeded`` marker.
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


# --- minimal SDK-shape fakes (mirror test_anthropic_sdk_tool_loop_iterations.py) ---


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
    return [dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == name]


# --- AC2: the loop-exceeded raise path emits a marked summary span -----------


@pytest.mark.asyncio
async def test_loop_exceeded_emits_summary_span_with_marker(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """A never-converging turn (tool every iteration) must STILL emit exactly one
    ``narrator.tool_loop`` summary span before ``AnthropicSdkLoopExceeded``
    raises — carrying ``iterations_used == max_iterations`` and
    ``loop_exceeded == True``. The worst-latency turn is no longer invisible to
    the metric."""
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
            max_iterations=4,
        )

    summary = _spans_named(otel_capture, _SUMMARY_SPAN)
    assert len(summary) == 1, (
        f"a ceiling-blown turn must emit exactly one {_SUMMARY_SPAN} span before "
        f"raising; got {len(summary)}"
    )
    assert summary[0].get("loop_exceeded") is True, (
        "the raise-path summary must mark loop_exceeded=True so the GM panel can "
        f"tell a ceiling-blown turn from a deep-but-converged one; got {summary[0]}"
    )
    assert summary[0].get("iterations_used") == 4, (
        "a loop-exceeded turn consumed all max_iterations round-trips; "
        f"iterations_used must equal max_iterations (4); got {summary[0]}"
    )


@pytest.mark.asyncio
async def test_loop_exceeded_summary_tracks_max_iterations(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """Non-vacuous: a different ``max_iterations`` ceiling records a different
    ``iterations_used`` on the raise-path summary — proves the count is the real
    ceiling, not a constant baked to 4."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeSdk(responses=[_tool_use_response() for _ in range(12)])
    client = AnthropicSdkClient(sdk=fake)

    with pytest.raises(AnthropicSdkLoopExceeded):
        await client.complete_with_tools(
            system_blocks=[CacheableBlock(text="rules", cache=True)],
            messages=[Message(role="user", content="loop forever")],
            tools=_ROLL_TOOL,
            tool_dispatch=_dispatch,
            model="claude-sonnet-4-6",
            max_iterations=7,
        )

    summary = _spans_named(otel_capture, _SUMMARY_SPAN)
    assert len(summary) == 1
    assert summary[0].get("iterations_used") == 7, (
        f"max_iterations=7 must record iterations_used=7 on raise; got {summary[0]}"
    )


@pytest.mark.asyncio
async def test_converged_summary_is_not_marked_loop_exceeded(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """The converged path's summary must NOT claim loop_exceeded — otherwise the
    marker is always-true and tells the GM panel nothing. A healthy
    tool-then-text turn is loop_exceeded=False/absent."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeSdk(responses=[_tool_use_response(), _text_response()])
    client = AnthropicSdkClient(sdk=fake)

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll then resolve")],
        tools=_ROLL_TOOL,
        tool_dispatch=_dispatch,
        model="claude-sonnet-4-6",
    )

    summary = _spans_named(otel_capture, _SUMMARY_SPAN)
    assert len(summary) == 1
    assert not summary[0].get("loop_exceeded"), (
        "a converged turn must not be marked loop_exceeded — the marker would be "
        f"vacuous if always true; got {summary[0]}"
    )


# --- AC5 hardening: cap-hit fires EXACTLY once across many post-cap iters -----


@pytest.mark.asyncio
async def test_cap_hit_span_fires_exactly_once_across_many_iterations(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """AC5 gap (exactly-once cap-hit): with cap=2 and a model that requests a
    tool every iteration up to max_iterations=6, the cap is crossed on iterations
    2,3,4,5,6 — but the cap-hit span must fire EXACTLY ONCE, not once per
    post-cap iteration. A per-iteration emit would spam the GM panel and
    overcount throttled turns."""
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
            max_iterations=6,
            iteration_cap=2,
        )

    cap_hits = _spans_named(otel_capture, _CAP_HIT_SPAN)
    assert len(cap_hits) == 1, (
        "the cap-hit span must fire exactly once per turn even when the cap is "
        f"crossed on every subsequent iteration; got {len(cap_hits)}"
    )
