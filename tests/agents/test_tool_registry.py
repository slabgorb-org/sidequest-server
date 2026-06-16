"""Tests for tool_registry primitives — ToolCategory, ToolContext, ToolResult."""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    Registry,
    ToolCategory,
    ToolContext,
    ToolResult,
    ToolResultStatus,
    tool,
)


def test_tool_result_ok_payload() -> None:
    r = ToolResult.ok({"hp": 12})
    assert r.status is ToolResultStatus.OK
    assert r.payload == {"hp": 12}
    assert r.message is None


def test_tool_result_not_found_carries_message() -> None:
    r = ToolResult.not_found("no monster named 'banana'")
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message == "no monster named 'banana'"


def test_tool_result_error_recoverable_default() -> None:
    r = ToolResult.error("validation failed")
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE
    assert r.message == "validation failed"


def test_tool_result_error_non_recoverable() -> None:
    r = ToolResult.error("db corrupt", recoverable=False)
    assert r.status is ToolResultStatus.ERROR_FATAL


def test_tool_result_to_anthropic_payload_ok() -> None:
    r = ToolResult.ok({"x": 1})
    body, is_error = r.to_anthropic_payload()
    assert is_error is False
    assert '"x"' in body


def test_tool_result_to_anthropic_payload_error_recoverable() -> None:
    r = ToolResult.error("nope")
    body, is_error = r.to_anthropic_payload()
    assert is_error is True
    assert "nope" in body


def test_tool_category_enum_values() -> None:
    assert ToolCategory.READ.value == "read"
    assert ToolCategory.WRITE.value == "write"
    assert ToolCategory.GENERATE.value == "generate"


def test_tool_context_is_frozen() -> None:
    from unittest.mock import MagicMock

    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=42,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=MagicMock(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.world_id = "different"  # type: ignore[misc]


def test_tool_context_perspective_pc_optional() -> None:
    from unittest.mock import MagicMock

    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc=None,
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=MagicMock(),
    )
    assert ctx.perspective_pc is None


def test_tool_result_with_unhashable_payload_raises_on_hash() -> None:
    """ToolResult is frozen but payloads may be unhashable (dict/list).

    Hashing fails loudly via TypeError. ToolResult is a value carrier, not
    a dict key — this test documents the contract."""
    r = ToolResult.ok({"hp": 12})
    with pytest.raises(TypeError):
        hash(r)


def test_tool_result_to_anthropic_payload_not_found() -> None:
    r = ToolResult.not_found("no such item")
    body, is_error = r.to_anthropic_payload()
    assert is_error is False  # NOT_FOUND is not an error
    assert "no such item" in body
    assert body.startswith("NOT_FOUND:")


def test_tool_result_to_anthropic_payload_error_fatal() -> None:
    r = ToolResult.error("db corrupt", recoverable=False)
    body, is_error = r.to_anthropic_payload()
    assert is_error is True
    assert "db corrupt" in body
    assert body.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Registry + @tool decorator tests
# ---------------------------------------------------------------------------


def _make_ctx() -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=_NoopFilter(),
    )


class _NoopFilter:
    def filter_result(
        self,
        *,
        tool_name: str,
        category: ToolCategory,
        result: ToolResult,
        perspective_pc: str | None,
    ) -> ToolResult:
        return result


class _SidesArgs(BaseModel):
    sides: int = Field(..., gt=0)


class _NoArgs(BaseModel):
    pass


async def test_registry_registers_and_lists_tools() -> None:
    reg = Registry()

    @tool(name="echo", description="echo", category=ToolCategory.READ, registry=reg)
    async def echo(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({})

    assert "echo" in reg.list_names()
    defs = reg.tool_definitions()
    assert any(d.name == "echo" for d in defs)


async def test_registry_dispatch_runs_handler() -> None:
    reg = Registry()

    @tool(name="roll", description="roll", category=ToolCategory.GENERATE, registry=reg)
    async def roll(args: _SidesArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({"value": args.sides // 2})

    from sidequest.agents.tooling_protocol import ToolUseBlock

    out = await reg.dispatch(
        ToolUseBlock(id="t1", name="roll", arguments={"sides": 20}),
        _make_ctx(),
    )
    assert out.tool_use_id == "t1"
    assert "10" in out.content
    assert out.is_error is False


async def test_registry_dispatch_returns_error_for_unknown_tool() -> None:
    reg = Registry()
    from sidequest.agents.tooling_protocol import ToolUseBlock

    out = await reg.dispatch(
        ToolUseBlock(id="t1", name="nope", arguments={}),
        _make_ctx(),
    )
    assert out.is_error is True
    assert "unknown tool" in out.content.lower()


async def test_registry_dispatch_rejects_bad_args() -> None:
    reg = Registry()

    @tool(name="roll", description="roll", category=ToolCategory.GENERATE, registry=reg)
    async def roll(args: _SidesArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({})

    from sidequest.agents.tooling_protocol import ToolUseBlock

    out = await reg.dispatch(
        ToolUseBlock(id="t1", name="roll", arguments={"sides": -1}),
        _make_ctx(),
    )
    assert out.is_error is True


async def test_registry_dispatch_serializes_writes_per_session() -> None:
    """Two parallel writes against one session run sequentially."""
    reg = Registry()
    order: list[str] = []

    @tool(name="write_a", description="a", category=ToolCategory.WRITE, registry=reg)
    async def w_a(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        order.append("a-start")
        await asyncio.sleep(0.02)
        order.append("a-end")
        return ToolResult.ok({})

    @tool(name="write_b", description="b", category=ToolCategory.WRITE, registry=reg)
    async def w_b(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        order.append("b-start")
        await asyncio.sleep(0.02)
        order.append("b-end")
        return ToolResult.ok({})

    from sidequest.agents.tooling_protocol import ToolUseBlock

    ctx = _make_ctx()
    await asyncio.gather(
        reg.dispatch(ToolUseBlock(id="1", name="write_a", arguments={}), ctx),
        reg.dispatch(ToolUseBlock(id="2", name="write_b", arguments={}), ctx),
    )
    # Sequential: a fully runs before b starts, or vice-versa.
    assert order in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )


async def test_registry_dispatch_parallelises_reads() -> None:
    """Reads on the same session may overlap."""
    reg = Registry()
    overlap = {"a_running": False, "b_saw_a": False}

    @tool(name="read_a", description="a", category=ToolCategory.READ, registry=reg)
    async def r_a(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        overlap["a_running"] = True
        await asyncio.sleep(0.02)
        overlap["a_running"] = False
        return ToolResult.ok({})

    @tool(name="read_b", description="b", category=ToolCategory.READ, registry=reg)
    async def r_b(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        if overlap["a_running"]:
            overlap["b_saw_a"] = True
        return ToolResult.ok({})

    from sidequest.agents.tooling_protocol import ToolUseBlock

    ctx = _make_ctx()
    await asyncio.gather(
        reg.dispatch(ToolUseBlock(id="1", name="read_a", arguments={}), ctx),
        asyncio.sleep(0.01),
    )

    # Confirm parallel-ability via direct overlap check.
    async def fire_pair() -> None:
        await asyncio.gather(
            reg.dispatch(ToolUseBlock(id="1", name="read_a", arguments={}), ctx),
            reg.dispatch(ToolUseBlock(id="2", name="read_b", arguments={}), ctx),
        )

    await fire_pair()
    assert overlap["b_saw_a"] is True


async def test_registry_dispatch_invokes_perception_filter() -> None:
    reg = Registry()
    seen: list[str] = []

    class _Tracking:
        def filter_result(
            self,
            *,
            tool_name: str,
            category: ToolCategory,
            result: ToolResult,
            perspective_pc: str | None,
        ) -> ToolResult:
            seen.append(tool_name)
            return result

    @tool(name="q", description="q", category=ToolCategory.READ, registry=reg)
    async def q(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({"x": 1})

    from sidequest.agents.tooling_protocol import ToolUseBlock

    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        store=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=_Tracking(),
    )
    await reg.dispatch(ToolUseBlock(id="t", name="q", arguments={}), ctx)
    assert seen == ["q"]


async def test_registry_register_rejects_duplicates() -> None:
    reg = Registry()

    @tool(name="dup", description="x", category=ToolCategory.READ, registry=reg)
    async def a(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({})

    with pytest.raises(ValueError, match="already registered"):

        @tool(name="dup", description="x", category=ToolCategory.READ, registry=reg)
        async def b(args: _NoArgs, ctx: ToolContext) -> ToolResult:
            return ToolResult.ok({})


async def test_registry_dispatch_converts_handler_exception_to_error_fatal() -> None:
    """Handler exceptions are caught and rendered as ERROR_FATAL ToolResults.

    Loud (recorded in OTEL + tool_result), but not terminal — the SDK loop
    continues so a single buggy tool doesn't abort the whole conversation."""
    reg = Registry()

    @tool(name="boom", description="x", category=ToolCategory.READ, registry=reg)
    async def boom(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("kaboom")

    from sidequest.agents.tooling_protocol import ToolUseBlock

    out = await reg.dispatch(
        ToolUseBlock(id="t1", name="boom", arguments={}),
        _make_ctx(),
    )
    assert out.is_error is True
    assert "kaboom" in out.content
    assert "RuntimeError" in out.content


async def test_registry_dispatch_unknown_tool_emits_span(otel_capture) -> None:
    """Unknown tool calls emit a tool.unknown.{name} span for GM visibility."""
    reg = Registry()
    from sidequest.agents.tooling_protocol import ToolUseBlock

    out = await reg.dispatch(
        ToolUseBlock(id="t1", name="phantom_tool", arguments={}),
        _make_ctx(),
    )
    assert out.is_error is True
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "tool.unknown.phantom_tool" in span_names
