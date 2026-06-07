"""Multi-text-block narration join fix (playtest 2026-06-07, five_points).

A single assistant message carrying MULTIPLE text blocks — a prose draft,
a tool_use, then a revised retelling — was joined with ``"".join(...)`` in
``complete_with_tools``, shipping BOTH tellings as one narration card (the
observed seam: *"…takes the measure.**Paradise Square…**"*). The fix keeps
only the LAST text block of each message and emits a WARNING-grade
``narrator.multi_text_block_discarded`` span when earlier blocks are
dropped — lie-detector visibility, no silent trimming.

Pins:
- final message ``[draft, revised]`` → ``result.text == revised`` + span
- final message ``[draft, tool_use, revised]`` shape via the tool loop
- single-text-block message → unchanged text, ZERO discard spans (no
  false positive on every ordinary turn)
- empty-text iteration still falls back to the prior iteration's text
  (``text or last_text`` carry-forward unchanged)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)


@dataclass
class _Usage:
    input_tokens: int = 10
    output_tokens: int = 10
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _Response:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str = "claude-sonnet-4-6"


class _Messages:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses

    async def create(self, **kwargs: Any) -> _Response:
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = _Messages(responses)


def _system() -> list[CacheableBlock]:
    return [CacheableBlock(text="system", cache=True)]


def _msgs() -> list[Message]:
    return [Message(role="user", content="I go into the store.")]


def _discard_spans(exporter: InMemorySpanExporter) -> list[Any]:
    return [
        s for s in exporter.get_finished_spans() if s.name == "narrator.multi_text_block_discarded"
    ]


@pytest.mark.asyncio
async def test_two_text_blocks_in_final_message_keeps_only_last(
    otel_capture: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doubled-card repro: [draft telling, revised telling] in ONE message
    must ship ONLY the revised telling, with a discard span as the audit trail."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    draft = "The store smells of tallow and takes the measure."
    revised = "**Paradise Square — The Five Corners**\n\nThe bell is a bent nail."
    sdk = _Sdk(
        responses=[
            _Response(
                content=[_TextBlock(text=draft), _TextBlock(text=revised)],
                stop_reason="end_turn",
                usage=_Usage(),
            )
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)

    result = await client.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[],
        model="claude-sonnet-4-6",
    )

    assert result.text == revised, (
        "narration must be the LAST text block only — joining drafts produced "
        "the 2026-06-07 doubled five_points card"
    )
    spans = _discard_spans(otel_capture)
    assert len(spans) == 1, "dropping a draft block must emit the discard span"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("discarded_count") == 1
    assert attrs.get("discarded_chars") == len(draft)
    assert attrs.get("kept_chars") == len(revised)


@pytest.mark.asyncio
async def test_draft_tool_use_revised_shape_through_tool_loop(
    otel_capture: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[draft, tool_use, revised] in one message: text blocks split around the
    tool_use; only the revised (last) block ships."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sdk = _Sdk(
        responses=[
            _Response(
                content=[
                    _TextBlock(text="A first telling of the scene."),
                    _ToolUseBlock(id="toolu_1", name="roll_dice", input={"sides": 20}),
                    _TextBlock(text="A second telling of the scene."),
                ],
                stop_reason="tool_use",
                usage=_Usage(),
            ),
            _Response(
                content=[_TextBlock(text="The converged narration.")],
                stop_reason="end_turn",
                usage=_Usage(),
            ),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)

    def dispatch(block: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="17")

    result = await client.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[
            ToolDefinition(
                name="roll_dice",
                description="Roll polyhedral dice",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        tool_dispatch=dispatch,
        model="claude-sonnet-4-6",
    )

    assert result.text == "The converged narration."
    spans = _discard_spans(otel_capture)
    assert len(spans) == 1, "the iter-1 draft block drop must be audited"
    assert dict(spans[0].attributes or {}).get("iteration") == 1


@pytest.mark.asyncio
async def test_single_text_block_emits_no_discard_span(
    otel_capture: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No false positives: the ordinary one-text-block turn is untouched."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sdk = _Sdk(
        responses=[
            _Response(
                content=[_TextBlock(text="A single clean telling.")],
                stop_reason="end_turn",
                usage=_Usage(),
            )
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)

    result = await client.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[],
        model="claude-sonnet-4-6",
    )

    assert result.text == "A single clean telling."
    assert _discard_spans(otel_capture) == []


@pytest.mark.asyncio
async def test_empty_text_iteration_carries_prior_text_forward(
    otel_capture: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``text or last_text`` carry-forward is unchanged: a final iteration with
    no text blocks falls back to the prior iteration's (last-block) text."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sdk = _Sdk(
        responses=[
            _Response(
                content=[
                    _TextBlock(text="Prose before the tool call."),
                    _ToolUseBlock(id="toolu_1", name="roll_dice", input={}),
                ],
                stop_reason="tool_use",
                usage=_Usage(),
            ),
            _Response(
                content=[],
                stop_reason="end_turn",
                usage=_Usage(),
            ),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)

    def dispatch(block: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="17")

    result = await client.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[
            ToolDefinition(
                name="roll_dice",
                description="Roll polyhedral dice",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        tool_dispatch=dispatch,
        model="claude-sonnet-4-6",
    )

    assert result.text == "Prose before the tool call."
    assert _discard_spans(otel_capture) == []
