"""Wiring test for Phase A — SDK client through a converged turn + spans.

Exercises the Phase A primitives together against the **claude-agent-sdk**
transport (Story 119-3): protocol dataclasses, the SDK client, usage/cost math
from ``ResultMessage.usage`` (a ``dict``), and ``llm.request`` span emission.

Story 119-3 transport changes that reshape this test:

* The manual ``messages.create`` loop is replaced by the SDK's ``query()`` loop,
  monkeypatched at the late-bound ``anthropic_sdk_client.query`` seam (OQ-9).
* ``AnthropicSdkClient()`` takes no args; both ``ANTHROPIC_API_KEY`` and
  ``ANTHROPIC_AUTH_TOKEN`` must be UNSET (a set key re-routes to PAYG and raises).
* ``cache_control`` markers / the ``extended-cache-ttl`` beta header are GONE —
  the CLI owns caching now (spec §6.4.3 / OQ-6), so the old request-payload
  assertions on those have no analog and are dropped.
* ``llm.request`` fires once per ``complete_with_tools`` call (not once per loop
  iteration — the SDK owns the loop), so a single span with ``llm.iteration==1``.

The model-driven tool round-trip (populated ``ToolingResult.tool_calls`` / a
WRITE tool firing through dispatch) cannot be driven by the hermetic fake
``query`` — the real SDK owns ``@tool`` invocation, so a converged stream yields
``tool_calls == []``. That bridge is covered by
``test_119_3_narrator_port.py::test_tool_bridge_dispatches_bare_name_and_accumulates``;
here the tool surface is still advertised, but the turn converges to prose.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents import anthropic_sdk_client
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)
from tests.agents.fakes.fake_agent_sdk import (
    FakeQuery,
    converged_text_stream,
    fake_usage,
)


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.mark.asyncio
async def test_combat_shaped_turn_wiring(
    otel_capture: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    prose = "The strike lands; the bandit reels."
    # A converged turn whose terminal ResultMessage carries the usage dict the
    # client adapts into token rollups + cost (spec §3.2 — usage is dict|None).
    fake = FakeQuery(
        converged_text_stream(
            text=prose,
            num_turns=3,
            usage=fake_usage(
                input_tokens=650,
                output_tokens=100,
                cache_read=24000,
                cache_write=0,
            ),
        )
    )
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    client = AnthropicSdkClient()

    def dispatch(block: ToolUseBlock) -> ToolResultBlock:
        # Defensive: the hermetic fake never invokes this (the SDK owns @tool
        # invocation), but the closure must satisfy the signature so the tool
        # surface is still advertised on the call.
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
    )

    # 1. Final narration came through; a converged success maps to end_turn.
    assert result.text == prose
    assert result.stop_reason == "end_turn"

    # 2. Token rollups are read from ResultMessage.usage (dict|None, §3.2).
    assert result.input_tokens == 650
    assert result.output_tokens == 100
    assert result.cached_input_read_tokens == 24000

    # 3. The tool surface was advertised (the call carried the SDK-MCP server),
    #    but a converged stream invokes no handler, so tool_calls is empty.
    #    The model-driven dispatch round-trip is covered by the port suite's
    #    test_tool_bridge_dispatches_bare_name_and_accumulates.
    options = fake.last_options
    assert options.allowed_tools == ["mcp__narration__roll_dice"], (
        "the ruleset-filtered tool catalog must surface as the SDK-MCP "
        f"allowed_tools; got {options.allowed_tools!r}"
    )
    assert result.tool_calls == []

    # 4. The system blocks collapse to a plain-string system_prompt (AC1) — the
    #    CLI owns caching, so the per-block cache markers / extended-cache-ttl
    #    beta header (removed symbols) have no request-payload analog.
    assert isinstance(options.system_prompt, str)
    assert "SOUL+rules+tone" in options.system_prompt

    # 5. One llm.request span emitted per complete_with_tools call (the SDK owns
    #    the loop, so it is no longer one-span-per-iteration).
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "llm.request"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["llm.iteration"] == 1
    assert attrs["llm.caller"] == "narrator"

    # 6. Cost attribute non-zero and computed against the cost module.
    cost_usd = attrs["llm.cost_usd"]
    assert isinstance(cost_usd, float) and cost_usd > 0
