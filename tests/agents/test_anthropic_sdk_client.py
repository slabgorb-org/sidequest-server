"""Tests for AnthropicSdkClient — construction, auth, error semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from sidequest.agents.anthropic_sdk_client import (
    AnthropicSdkClient,
    AnthropicSdkClientError,
    AnthropicSdkConfigError,
)
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolingLlmClient,
    ToolResultBlock,
    ToolUseBlock,
)


def test_construction_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AnthropicSdkConfigError):
        AnthropicSdkClient()


def test_construction_reads_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1")
    client = AnthropicSdkClient()
    assert client.api_key_present is True


def test_construction_accepts_explicit_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake_sdk = MagicMock(name="AsyncAnthropic")
    client = AnthropicSdkClient(sdk=fake_sdk)
    assert client.api_key_present is False  # bypassed via explicit injection
    assert client._sdk is fake_sdk  # type: ignore[attr-defined]


def test_implements_tooling_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1")
    client = AnthropicSdkClient()
    assert isinstance(client, ToolingLlmClient)


def test_default_cache_ttl_is_1_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit-and-wait MP cadence exceeds the 5m window; the operative
    default is 1h so the ~30k stable system prefix amortizes across an
    ~85-turn session instead of being re-written almost every turn."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1")
    monkeypatch.delenv("SIDEQUEST_ANTHROPIC_CACHE_TTL", raising=False)
    client = AnthropicSdkClient()
    assert client.cache_ttl == "1h"


def test_opt_into_1_hour_cache_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """1h is restored as a valid TTL (paired with the extended-cache-ttl
    beta header on the request — see the wiring tests below)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1")
    monkeypatch.setenv("SIDEQUEST_ANTHROPIC_CACHE_TTL", "1h")
    client = AnthropicSdkClient()
    assert client.cache_ttl == "1h"


def test_explicit_5m_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """5m stays selectable for operators who want the short window."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1")
    monkeypatch.setenv("SIDEQUEST_ANTHROPIC_CACHE_TTL", "5m")
    client = AnthropicSdkClient()
    assert client.cache_ttl == "5m"


def test_invalid_cache_ttl_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1")
    monkeypatch.setenv("SIDEQUEST_ANTHROPIC_CACHE_TTL", "banana")
    with pytest.raises(AnthropicSdkConfigError):
        AnthropicSdkClient()


# --- SDK-shape fake for complete_with_tools loop tests ----------------------


@dataclass(frozen=True)
class _CacheCreation:
    ephemeral_5m_input_tokens: int = 0
    ephemeral_1h_input_tokens: int = 0


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: _CacheCreation | None = None


@dataclass(frozen=True)
class _SdkContentTextBlock:
    type: str
    text: str


@dataclass(frozen=True)
class _SdkContentToolUseBlock:
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


class _FakeSdkMessages:
    def __init__(self, responses: list[_SdkResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _SdkResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeSdkMessages: out of scripted responses")
        return self._responses.pop(0)


class _FakeAsyncSdk:
    def __init__(self, responses: list[_SdkResponse]) -> None:
        self.messages = _FakeSdkMessages(responses)


async def test_complete_with_tools_simple_end_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk_response = _SdkResponse(
        content=[_SdkContentTextBlock(type="text", text="The lantern gutters.")],
        stop_reason="end_turn",
        usage=_Usage(
            input_tokens=100,
            output_tokens=8,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=0,
        ),
        model="claude-sonnet-4-6",
    )
    fake = _FakeAsyncSdk(responses=[sdk_response])
    client = AnthropicSdkClient(sdk=fake)
    result = await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    assert result.text == "The lantern gutters."
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 100
    assert result.cached_input_read_tokens == 80
    assert len(fake.messages.calls) == 1


async def test_complete_with_tools_cache_control_on_last_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk_response = _SdkResponse(
        content=[_SdkContentTextBlock(type="text", text="ok")],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=10, output_tokens=2),
        model="claude-sonnet-4-6",
    )
    fake = _FakeAsyncSdk(responses=[sdk_response])
    client = AnthropicSdkClient(sdk=fake)
    await client.complete_with_tools(
        system_blocks=[
            CacheableBlock(text="zone 1", cache=True),
            CacheableBlock(text="zone 2", cache=True),
            CacheableBlock(text="zone 3", cache=False),
        ],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    call = fake.messages.calls[0]
    system = call["system"]
    # Two cache-marked blocks get cache_control markers.
    assert system[0]["cache_control"]["type"] == "ephemeral"
    assert system[1]["cache_control"]["type"] == "ephemeral"
    assert "cache_control" not in system[2]


def _one_block_call(client: AnthropicSdkClient, fake: _FakeAsyncSdk):
    """Drive a single end_turn call and return the recorded create() kwargs."""
    return fake.messages.calls[0]


async def test_cache_control_marker_carries_1h_ttl_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cache_control echoes self.cache_ttl — a 1h client emits ttl:'1h'."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeAsyncSdk(
        responses=[
            _SdkResponse(
                content=[_SdkContentTextBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=fake, cache_ttl="1h")
    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="stable scaffold", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    call = _one_block_call(client, fake)
    assert call["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


async def test_cache_control_marker_carries_5m_ttl_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No special-casing: an explicit 5m client emits ttl:'5m' (a valid
    value to the API), not a bare ephemeral marker."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeAsyncSdk(
        responses=[
            _SdkResponse(
                content=[_SdkContentTextBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=fake, cache_ttl="5m")
    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="stable scaffold", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    call = _one_block_call(client, fake)
    assert call["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


async def test_extended_cache_ttl_beta_header_sent_when_1h(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ttl:'1h' requires the extended-cache-ttl beta header or the API
    rejects the request — assert it rides every messages.create call."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeAsyncSdk(
        responses=[
            _SdkResponse(
                content=[_SdkContentTextBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=fake, cache_ttl="1h")
    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="stable scaffold", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    call = _one_block_call(client, fake)
    assert call["extra_headers"]["anthropic-beta"] == "extended-cache-ttl-2025-04-11"


async def test_no_beta_header_on_5m(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5m needs no beta — the 5m request path stays identical to today."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeAsyncSdk(
        responses=[
            _SdkResponse(
                content=[_SdkContentTextBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=fake, cache_ttl="5m")
    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="stable scaffold", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    call = _one_block_call(client, fake)
    extra = call.get("extra_headers") or {}
    assert "anthropic-beta" not in extra


async def test_complete_with_tools_runs_tool_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    first = _SdkResponse(
        content=[
            _SdkContentToolUseBlock(
                type="tool_use",
                id="toolu_1",
                name="roll_dice",
                input={"sides": 20},
            )
        ],
        stop_reason="tool_use",
        usage=_Usage(input_tokens=200, output_tokens=15),
        model="claude-sonnet-4-6",
    )
    second = _SdkResponse(
        content=[_SdkContentTextBlock(type="text", text="The roll landed.")],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=220, output_tokens=10),
        model="claude-sonnet-4-6",
    )
    fake = _FakeAsyncSdk(responses=[first, second])
    client = AnthropicSdkClient(sdk=fake)

    def dispatch(block: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="17", is_error=False)

    result = await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll for it")],
        tools=[
            ToolDefinition(
                name="roll_dice",
                description="Roll",
                input_schema={"type": "object"},
            )
        ],
        tool_dispatch=dispatch,
        model="claude-sonnet-4-6",
    )
    assert result.text == "The roll landed."
    assert result.stop_reason == "end_turn"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "roll_dice"
    assert len(fake.messages.calls) == 2


async def test_complete_with_tools_respects_max_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Infinite tool-use loop (would be a bug in the model in real life).
    def loop_response() -> _SdkResponse:
        return _SdkResponse(
            content=[_SdkContentToolUseBlock(type="tool_use", id="x", name="roll_dice", input={})],
            stop_reason="tool_use",
            usage=_Usage(input_tokens=10, output_tokens=1),
            model="claude-sonnet-4-6",
        )

    fake = _FakeAsyncSdk(responses=[loop_response() for _ in range(20)])
    client = AnthropicSdkClient(sdk=fake)

    def dispatch(block: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="ok")

    with pytest.raises(AnthropicSdkClientError):
        await client.complete_with_tools(
            system_blocks=[CacheableBlock(text="r")],
            messages=[Message(role="user", content="hi")],
            tools=[
                ToolDefinition(name="roll_dice", description="r", input_schema={"type": "object"})
            ],
            tool_dispatch=dispatch,
            model="claude-sonnet-4-6",
            max_iterations=3,
        )


async def test_complete_with_tools_records_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cost arithmetic happens via compute_cost_usd; client surfaces buckets."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk_response = _SdkResponse(
        content=[_SdkContentTextBlock(type="text", text="x")],
        stop_reason="end_turn",
        usage=_Usage(
            input_tokens=200,
            output_tokens=100,
            cache_read_input_tokens=800,
            cache_creation_input_tokens=50,
        ),
        model="claude-sonnet-4-6",
    )
    fake = _FakeAsyncSdk(responses=[sdk_response])
    client = AnthropicSdkClient(sdk=fake)
    result = await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="r", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    assert result.cached_input_read_tokens == 800
    assert result.cached_input_write_tokens == 50


async def test_complete_with_tools_imports_anthropic_sdk_error_types() -> None:
    """Ensure AnthropicSdkClientError exists and is wired."""
    assert issubclass(AnthropicSdkClientError, Exception)


def test_tooling_result_has_ttl_breakdown_fields() -> None:
    """ToolingResult exposes per-TTL write breakdowns so the orchestrator
    can attribute 5m vs 1h cache writes onto narration.turn spans."""
    from sidequest.agents.tooling_protocol import ToolingResult

    result = ToolingResult(
        text="ok",
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=2,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
        cached_input_write_5m_tokens=100,
        cached_input_write_1h_tokens=200,
    )
    assert result.cached_input_write_5m_tokens == 100
    assert result.cached_input_write_1h_tokens == 200


def test_tooling_result_breakdown_fields_default_to_zero() -> None:
    """Legacy test fixtures that construct ToolingResult by hand without
    the new fields keep working."""
    from sidequest.agents.tooling_protocol import ToolingResult

    result = ToolingResult(
        text="ok",
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=2,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
    )
    assert result.cached_input_write_5m_tokens == 0
    assert result.cached_input_write_1h_tokens == 0


async def test_ttl_breakdown_flows_into_tooling_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """usage.cache_creation.ephemeral_{5m,1h}_input_tokens reach ToolingResult."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk_response = _SdkResponse(
        content=[_SdkContentTextBlock(type="text", text="ok")],
        stop_reason="end_turn",
        usage=_Usage(
            input_tokens=10,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=300,
            cache_creation=_CacheCreation(
                ephemeral_5m_input_tokens=100,
                ephemeral_1h_input_tokens=200,
            ),
        ),
        model="claude-sonnet-4-6",
    )
    fake = _FakeAsyncSdk(responses=[sdk_response])
    client = AnthropicSdkClient(sdk=fake)
    result = await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="x", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    assert result.cached_input_write_5m_tokens == 100
    assert result.cached_input_write_1h_tokens == 200
    # Aggregate stays unchanged.
    assert result.cached_input_write_tokens == 300


async def test_ttl_breakdown_defaults_zero_when_field_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK-version-drift case: older SDKs return no `cache_creation` object.
    The breakdown silently goes to 0; the aggregate field stays correct."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk_response = _SdkResponse(
        content=[_SdkContentTextBlock(type="text", text="ok")],
        stop_reason="end_turn",
        usage=_Usage(
            input_tokens=10,
            output_tokens=2,
            cache_creation_input_tokens=500,
            cache_creation=None,  # older SDK shape
        ),
        model="claude-sonnet-4-6",
    )
    fake = _FakeAsyncSdk(responses=[sdk_response])
    client = AnthropicSdkClient(sdk=fake)
    result = await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="x", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    assert result.cached_input_write_5m_tokens == 0
    assert result.cached_input_write_1h_tokens == 0
    assert result.cached_input_write_tokens == 500


async def test_per_iter_log_line_includes_ttl_breakdown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """narrator.sdk.usage log line gains `5m=N 1h=N` columns so cache
    breakdown is visible in /tmp/sidequest-server.log without a WS tap."""
    import logging

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk_response = _SdkResponse(
        content=[_SdkContentTextBlock(type="text", text="ok")],
        stop_reason="end_turn",
        usage=_Usage(
            input_tokens=10,
            output_tokens=2,
            cache_creation_input_tokens=300,
            cache_creation=_CacheCreation(
                ephemeral_5m_input_tokens=100,
                ephemeral_1h_input_tokens=200,
            ),
        ),
        model="claude-sonnet-4-6",
    )
    fake = _FakeAsyncSdk(responses=[sdk_response])
    client = AnthropicSdkClient(sdk=fake)
    with caplog.at_level(logging.INFO, logger="sidequest.agents.anthropic_sdk_client"):
        await client.complete_with_tools(
            system_blocks=[CacheableBlock(text="x", cache=True)],
            messages=[Message(role="user", content="hi")],
            tools=[],
            model="claude-sonnet-4-6",
        )
    usage_records = [r for r in caplog.records if "narrator.sdk.usage" in r.getMessage()]
    assert usage_records, "expected at least one narrator.sdk.usage log line"
    msg = usage_records[0].getMessage()
    assert "5m=100" in msg, f"expected '5m=100' in log line; got: {msg}"
    assert "1h=200" in msg, f"expected '1h=200' in log line; got: {msg}"


async def test_last_tool_gets_cache_control_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last tool definition carries a cache_control marker with the
    client's configured TTL. Earlier tools do not. This converts the
    tools array (byte-stable across every turn) into an explicit 1h
    cache prefix instead of relying on Anthropic's default 5m auto-cache."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeAsyncSdk(
        responses=[
            _SdkResponse(
                content=[_SdkContentTextBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=fake, cache_ttl="1h")
    tools = [
        ToolDefinition(name="alpha", description="a", input_schema={"type": "object"}),
        ToolDefinition(name="beta", description="b", input_schema={"type": "object"}),
        ToolDefinition(name="gamma", description="c", input_schema={"type": "object"}),
    ]
    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="x", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=tools,
        model="claude-sonnet-4-6",
    )
    sent_tools = fake.messages.calls[0]["tools"]
    assert len(sent_tools) == 3
    assert "cache_control" not in sent_tools[0]
    assert "cache_control" not in sent_tools[1]
    assert sent_tools[2]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


async def test_last_tool_marker_inherits_5m_ttl_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No parallel TTL config — the tools marker echoes the same
    self.cache_ttl as the system-block marker."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeAsyncSdk(
        responses=[
            _SdkResponse(
                content=[_SdkContentTextBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=fake, cache_ttl="5m")
    tools = [
        ToolDefinition(name="alpha", description="a", input_schema={"type": "object"}),
    ]
    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="x", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=tools,
        model="claude-sonnet-4-6",
    )
    sent_tools = fake.messages.calls[0]["tools"]
    assert sent_tools[0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


async def test_empty_tools_array_skips_marker_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test fixtures sometimes pass tools=[]. The marker is best-effort
    opt-in; an empty array must not raise. Production has 27 tools."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = _FakeAsyncSdk(
        responses=[
            _SdkResponse(
                content=[_SdkContentTextBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=fake, cache_ttl="1h")
    # Must not raise.
    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="x", cache=True)],
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="claude-sonnet-4-6",
    )
    sent_tools = fake.messages.calls[0]["tools"]
    assert sent_tools == []
