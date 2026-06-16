"""Phase B wiring test — registry + SDK-client tool bridge + dispatch round-trip.

Story 119-3: the narrator transport is ``claude-agent-sdk``. The model's tool
calls reach the registry through the in-process SDK-MCP ``@tool`` bridge built by
``AnthropicSdkClient._build_narration_mcp`` — each tool gets a
``_build_narration_tool_handler`` whose body re-enters the orchestrator's
``tool_dispatch`` closure (which calls ``Registry.dispatch``) and returns the
result in the SDK ``{"content":[...],"is_error":...}`` shape.

The hermetic fake ``query`` does NOT invoke ``@tool`` handlers (the real SDK
owns the loop), so a converged stream yields ``ToolingResult.tool_calls == []``
and cannot exercise the registry round-trip end-to-end. This file's load-bearing
seam — that a model tool call flows through the SDK client into the REAL
``Registry.dispatch`` and the result flows back — is therefore pinned on the
production bridge handler driven against the real registry, which is exactly the
callable the SDK loop invokes. (The convergence-side of the loop is covered by
``test_119_3_narrator_port.py``.)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field

from sidequest.agents.anthropic_sdk_client import _build_narration_tool_handler
from sidequest.agents.perception_filter import NoopPerceptionFilter
from sidequest.agents.tool_registry import (
    Registry,
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock


class _DiceArgs(BaseModel):
    sides: int = Field(..., gt=0)


async def test_registry_round_trip_via_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model tool call routes through the SDK client's ``@tool`` bridge into
    the REAL ``Registry.dispatch`` and the dispatch result flows back to the SDK.

    Driven on the production bridge (``_build_narration_tool_handler``) against
    the real registry rather than a converged fake ``query`` — the hermetic fake
    never fires ``@tool`` handlers (spec §tool-dispatch limitation), so the
    end-to-end round-trip can only be exercised at the bridge boundary the SDK
    loop calls.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    reg = Registry()

    @tool(
        name="roll_dice",
        description="Roll dice.",
        category=ToolCategory.GENERATE,
        registry=reg,
    )
    async def roll(args: _DiceArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({"value": args.sides})

    ctx = ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="alex",
        turn_number=1,
        repository=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NoopPerceptionFilter(),
    )

    async def dispatch(block: ToolUseBlock) -> ToolResultBlock:
        return await reg.dispatch(block, ctx)

    # The exact bridge AnthropicSdkClient._build_narration_mcp builds per tool:
    # the SDK loop calls this handler with the model's args.
    accumulator: list[ToolUseBlock] = []
    handler = _build_narration_tool_handler(
        bare_name="roll_dice",
        tool_dispatch=dispatch,
        accumulator=accumulator,
    )

    sdk_result = await handler({"sides": 20})

    # The model's call reached the real registry under its bare name and was
    # accumulated onto the tool-call ledger (ToolingResult.tool_calls source).
    assert [b.name for b in accumulator] == ["roll_dice"]
    assert accumulator[0].arguments == {"sides": 20}

    # The dispatch result flowed back in the SDK content shape, not an error.
    assert sdk_result.get("is_error") is False
    content = sdk_result.get("content")
    assert isinstance(content, list) and content
    assert content[0].get("type") == "text"
    assert '"value": 20' in content[0].get("text", "")


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
        repository=MagicMock(),
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
        repository=MagicMock(),
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
        repository=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NoopPerceptionFilter(),
        lore_store=store,
    )
    assert ctx.lore_store is store
