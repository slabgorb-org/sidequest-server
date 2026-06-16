"""Phase B wiring test — registry + SDK client + dispatch round-trip."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field

from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.perception_filter import NoopPerceptionFilter
from sidequest.agents.tool_registry import (
    Registry,
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
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
class _Text:
    type: str
    text: str


@dataclass
class _ToolUse:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _Resp:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _Msgs:
    def __init__(self, responses: list[_Resp]) -> None:
        self._r = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return self._r.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


class _DiceArgs(BaseModel):
    sides: int = Field(..., gt=0)


async def test_registry_round_trip_via_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reg = Registry()

    @tool(
        name="roll_dice",
        description="Roll dice.",
        category=ToolCategory.GENERATE,
        registry=reg,
    )
    async def roll(args: _DiceArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({"value": args.sides})

    sdk = _Sdk(
        responses=[
            _Resp(
                content=[
                    _ToolUse(
                        type="tool_use",
                        id="t1",
                        name="roll_dice",
                        input={"sides": 20},
                    )
                ],
                stop_reason="tool_use",
                usage=_Usage(input_tokens=200, output_tokens=15),
                model="claude-sonnet-4-6",
            ),
            _Resp(
                content=[_Text(type="text", text="A natural 20.")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=220, output_tokens=10),
                model="claude-sonnet-4-6",
            ),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)
    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NoopPerceptionFilter(),
    )

    async def dispatch(block: ToolUseBlock) -> ToolResultBlock:
        return await reg.dispatch(block, ctx)

    result = await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="roll d20")],
        tools=reg.tool_definitions(),
        tool_dispatch=dispatch,
        model="claude-sonnet-4-6",
    )
    assert result.text == "A natural 20."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "roll_dice"


class _MarkerArgs(BaseModel):
    """Args for the marker tool used by the dispatch-span wiring test."""


async def test_dispatch_injects_span_into_handler_context(otel_capture) -> None:
    """Handlers' ctx.otel_span MUST be the dispatch span, not the caller's span.

    Without dispatch-span injection, per-tool attrs set by handlers via
    ctx.otel_span.set_attribute land on the caller-supplied span (typically
    a MagicMock or a logically unrelated span) and the GM panel — which
    watches tool.{read,write,gen}.{name} — sees no per-tool detail.
    """
    reg = Registry()

    @tool(
        name="marker",
        description="Stamp a marker attribute via ctx.otel_span.",
        category=ToolCategory.READ,
        registry=reg,
    )
    async def _marker(args: _MarkerArgs, ctx: ToolContext) -> ToolResult:
        ctx.otel_span.set_attribute("tool.test.marker", "yes")
        return ToolResult.ok({"ok": True})

    caller_span = MagicMock()
    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        store=MagicMock(),
        otel_span=caller_span,
        perception_filter=NoopPerceptionFilter(),
    )

    out = await reg.dispatch(
        ToolUseBlock(id="t-marker", name="marker", arguments={}),
        ctx,
    )
    assert out.is_error is False

    # The caller-supplied span must NOT have received tool.test.marker —
    # handlers must write through the dispatch span injected by Registry.
    marker_calls = [
        c
        for c in caller_span.set_attribute.call_args_list
        if c.args and c.args[0] == "tool.test.marker"
    ]
    assert not marker_calls, (
        "ctx.otel_span was not replaced with the dispatch span — "
        f"caller span received tool.test.marker: {marker_calls}"
    )

    spans = otel_capture.get_finished_spans()
    marker_spans = [s for s in spans if s.name == "tool.read.marker"]
    assert marker_spans, (
        f"no tool.read.marker dispatch span exported; got: {[s.name for s in spans]}"
    )
    attrs = dict(marker_spans[-1].attributes or {})
    assert attrs.get("tool.name") == "marker"
    assert attrs.get("tool.category") == "read"
    assert attrs.get("tool.test.marker") == "yes", (
        f"handler attribute did not land on dispatch span; attrs={attrs}"
    )


def test_tool_context_lore_store_defaults_to_none() -> None:
    """Phase C Task 13 amendment: ``lore_store`` is optional, defaults to None.

    Existing test constructors that omit the field must continue to work.
    Production wiring (Phase E) will set this to the session-handler's
    LoreStore at the ctx construction site.
    """
    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NoopPerceptionFilter(),
    )
    assert ctx.lore_store is None


def test_tool_context_accepts_lore_store_kwarg() -> None:
    """Phase E call site will pass a LoreStore — verify the dataclass slot."""
    from sidequest.game.lore_store import LoreStore

    store = LoreStore()
    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NoopPerceptionFilter(),
        lore_store=store,
    )
    assert ctx.lore_store is store
