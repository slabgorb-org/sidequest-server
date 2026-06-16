"""Wiring test for Phase A — SDK client through tool round-trip + spans.

Exercises every Phase A primitive together: protocol dataclasses, the
SDK client, cost math, llm.request span emission, cache_control on
system blocks, the tool loop, and streaming text deltas.
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
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _TextBlock:
    type: str
    text: str


@dataclass
class _ToolUseBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _Response:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _Messages:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.received: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Response:
        self.received.append(kwargs)
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = _Messages(responses)


@pytest.mark.asyncio
async def test_combat_shaped_turn_wiring(
    otel_capture: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SIDEQUEST_ANTHROPIC_CACHE_TTL", raising=False)

    sdk = _Sdk(
        responses=[
            _Response(
                content=[
                    _ToolUseBlock(
                        type="tool_use",
                        id="toolu_a",
                        name="roll_dice",
                        input={"sides": 20},
                    )
                ],
                stop_reason="tool_use",
                usage=_Usage(
                    input_tokens=300,
                    output_tokens=20,
                    cache_read_input_tokens=12000,
                    cache_creation_input_tokens=0,
                ),
                model="claude-sonnet-4-6",
            ),
            _Response(
                content=[
                    _TextBlock(
                        type="text",
                        text="The strike lands; the bandit reels.",
                    )
                ],
                stop_reason="end_turn",
                usage=_Usage(
                    input_tokens=350,
                    output_tokens=80,
                    cache_read_input_tokens=12000,
                    cache_creation_input_tokens=0,
                ),
                model="claude-sonnet-4-6",
            ),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)

    deltas: list[str] = []

    def dispatch(block: ToolUseBlock) -> ToolResultBlock:
        assert block.name == "roll_dice"
        return ToolResultBlock(tool_use_id=block.id, content="17")

    result = await client.complete_with_tools(
        system_blocks=[
            CacheableBlock(text="SOUL+rules+tone", cache=True),
            CacheableBlock(text="tool defs", cache=True),
            CacheableBlock(text="world snapshot", cache=True),
        ],
        messages=[
            Message(role="user", content="I swing for the bandit."),
        ],
        tools=[
            ToolDefinition(
                name="roll_dice",
                description="Roll polyhedral dice",
                input_schema={
                    "type": "object",
                    "properties": {"sides": {"type": "integer"}},
                    "required": ["sides"],
                },
            )
        ],
        tool_dispatch=dispatch,
        model="claude-sonnet-4-6",
        on_text_delta=deltas.append,
    )

    # 1. Final narration came through.
    assert result.text == "The strike lands; the bandit reels."
    assert result.stop_reason == "end_turn"

    # 2. Token rollups are cumulative across iterations.
    assert result.input_tokens == 650
    assert result.output_tokens == 100
    assert result.cached_input_read_tokens == 24000

    # 3. Tool round-trip captured.
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "roll_dice"

    # 4. Streaming callback got the final-turn text.
    assert deltas == ["The strike lands; the bandit reels."]

    # 5. Default path is now 1h: the real request payload carries
    #    ttl:"1h" on the cache_control marker, and the extended-cache-ttl
    #    beta header rides every messages.create call (without it the API
    #    400s the 1h request — see test_anthropic_sdk_client.py).
    first_call = sdk.messages.received[0]
    sys_array = first_call["system"]
    assert sys_array[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert first_call["extra_headers"]["anthropic-beta"] == "extended-cache-ttl-2025-04-11"

    # 6. Two llm.request spans emitted (one per iteration).
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "llm.request"]
    assert len(spans) == 2
    iter_attrs = sorted(int(str((s.attributes or {})["llm.iteration"])) for s in spans)
    assert iter_attrs == [1, 2]

    # 7. Cost attribute non-zero and computed against the cost module.
    first_attrs = dict(spans[0].attributes or {})
    cost_usd = first_attrs["llm.cost_usd"]
    assert isinstance(cost_usd, float) and cost_usd > 0
