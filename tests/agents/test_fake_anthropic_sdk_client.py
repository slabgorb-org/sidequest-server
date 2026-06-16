"""Tests for the FakeAnthropicSdkClient test double."""

from __future__ import annotations

import pytest

from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolingLlmClient,
    ToolResultBlock,
    ToolUseBlock,
)
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
    ScriptExhausted,
)


def _system() -> list[CacheableBlock]:
    return [CacheableBlock(text="SYSTEM RULES", cache=True)]


def _msgs() -> list[Message]:
    return [Message(role="user", content="What happens next?")]


async def test_fake_returns_scripted_text() -> None:
    fake = FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text="The lantern gutters.",
                stop_reason="end_turn",
                input_tokens=100,
                output_tokens=8,
                cached_input_read_tokens=80,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
            )
        ]
    )
    result = await fake.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[],
        model="claude-sonnet-4-6",
    )
    assert result.text == "The lantern gutters."
    assert result.stop_reason == "end_turn"
    assert result.cached_input_read_tokens == 80


async def test_fake_implements_protocol() -> None:
    fake = FakeAnthropicSdkClient(responses=[])
    assert isinstance(fake, ToolingLlmClient)


async def test_fake_runs_tool_loop() -> None:
    """Script a tool_use response, then a final end_turn response."""
    tool_use = ToolUseBlock(id="toolu_x", name="roll_dice", arguments={"sides": 20})
    fake = FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text="",
                stop_reason="tool_use",
                input_tokens=200,
                output_tokens=15,
                cached_input_read_tokens=180,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
                tool_uses=[tool_use],
            ),
            ScriptedResponse(
                text="The roll landed.",
                stop_reason="end_turn",
                input_tokens=220,
                output_tokens=10,
                cached_input_read_tokens=180,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
            ),
        ]
    )

    def dispatch(block: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="17", is_error=False)

    tools = [
        ToolDefinition(
            name="roll_dice",
            description="Roll dice.",
            input_schema={"type": "object", "properties": {}, "required": []},
        )
    ]
    result = await fake.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=tools,
        tool_dispatch=dispatch,
        model="claude-sonnet-4-6",
    )
    assert result.text == "The roll landed."
    assert result.stop_reason == "end_turn"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "roll_dice"


async def test_fake_records_request_payloads() -> None:
    fake = FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text="ok",
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=2,
                cached_input_read_tokens=0,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
            )
        ]
    )
    await fake.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[],
        model="claude-sonnet-4-6",
    )
    assert len(fake.recorded_requests) == 1
    req = fake.recorded_requests[0]
    assert req.model == "claude-sonnet-4-6"
    assert req.system_blocks == _system()
    assert req.messages == _msgs()


async def test_fake_raises_when_script_exhausted() -> None:
    fake = FakeAnthropicSdkClient(responses=[])
    with pytest.raises(ScriptExhausted):
        await fake.complete_with_tools(
            system_blocks=_system(),
            messages=_msgs(),
            tools=[],
            model="claude-sonnet-4-6",
        )


async def test_fake_streams_text_deltas() -> None:
    deltas: list[str] = []
    fake = FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text="The lantern gutters.",
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=4,
                cached_input_read_tokens=0,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
                stream_deltas=["The lantern", " gutters."],
            )
        ]
    )
    await fake.complete_with_tools(
        system_blocks=_system(),
        messages=_msgs(),
        tools=[],
        model="claude-sonnet-4-6",
        on_text_delta=deltas.append,
    )
    assert deltas == ["The lantern", " gutters."]
