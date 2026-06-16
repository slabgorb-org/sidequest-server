"""Wiring test for Phase D Task 1 — Orchestrator routes through the SDK + Registry.

When the orchestrator's LlmClient is a ToolingLlmClient (an
AnthropicSdkClient in production), ``run_narration_turn`` must:

* Go through ``AnthropicSdkClient.complete_with_tools``.
* Advertise the full 41-tool array from ``default_registry`` (Story 119-3: the
  catalog surfaces as the SDK-MCP ``allowed_tools`` on the ClaudeAgentOptions).
* Open a ``narration.turn`` cost-rollup span and seed the rollup attributes
  (model, token totals, tool-call count).
* Return a ``NarrationTurnResult`` whose ``narration`` field matches the SDK's
  text output.

Story 119-3: the transport is ``claude-agent-sdk``. The orchestrator drives one
``query()`` call per turn (the SDK owns the tool loop), monkeypatched at the
late-bound ``anthropic_sdk_client.query`` seam. The token rollups are read from
the terminal ``ResultMessage.usage`` dict (no longer summed across hand-driven
``messages.create`` iterations). The hermetic fake ``query`` does NOT invoke
``@tool`` handlers, so the perception-filter-reaches-the-registry wiring is
exercised by invoking the production ``tool_dispatch`` closure the orchestrator
hands to ``complete_with_tools`` — the same closure the SDK-MCP bridge calls.

The test monkeypatches ``build_narrator_prompt`` so it does not have to exercise
the full prompt builder — the seam under test is the SDK routing path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Importing the tools package wires the 41 adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents import anthropic_sdk_client
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolingResult,
    ToolResultBlock,
    ToolUseBlock,
)
from tests.agents.fakes.fake_agent_sdk import FakeQuery, converged_text_stream, fake_usage


class _FakeRegistry:
    """Stand-in for PromptRegistry.compose_split with the minimum API we use."""

    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        # Story 60-2: the SDK path emits an enriched prompt_assembled post-call
        # that reads registry sections; this stub registers none.
        return []


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.mark.asyncio
async def test_orchestrator_routes_narration_through_sdk(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: InMemorySpanExporter,
) -> None:
    """When the LlmClient is a ToolingLlmClient, run_narration_turn must
    funnel through complete_with_tools with the full tool catalog and
    populate the narration.turn span rollup attributes.
    """
    fake_response_text = "The wind rises across the salt flats."
    # The terminal ResultMessage.usage dict carries the turn's token totals.
    fake = FakeQuery(
        converged_text_stream(
            text=fake_response_text,
            num_turns=2,
            usage=fake_usage(
                input_tokens=450,
                output_tokens=72,
                cache_read=4800,
                cache_write=120,
            ),
        )
    )
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    client = AnthropicSdkClient()
    orch = Orchestrator(client=client)

    # The hermetic fake query does not invoke @tool handlers (the real SDK owns
    # that), so capture the production tool_dispatch closure the orchestrator
    # hands to complete_with_tools and invoke it once ourselves — exercising the
    # SAME path the SDK-MCP bridge would, so the ToolContext (with its
    # NarratorPerceptionFilter) is built and reaches default_registry.dispatch.
    captured_ctx: list[ToolContext] = []

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        captured_ctx.append(ctx)
        return ToolResultBlock(tool_use_id=block.id, content="17", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    original_complete = client.complete_with_tools

    async def _complete_spy(
        system_blocks: list[CacheableBlock],
        messages: list[Message],
        tools: list[ToolDefinition],
        tool_dispatch: Callable[[ToolUseBlock], Awaitable[ToolResultBlock] | ToolResultBlock]
        | None = None,
        **kwargs: Any,
    ) -> ToolingResult:
        # Fire the real dispatch closure exactly as the SDK-MCP bridge would,
        # so the orchestrator's tool_ctx flows into default_registry.dispatch.
        if tool_dispatch is not None:
            await tool_dispatch(ToolUseBlock(id="toolu_1", name="roll_dice", arguments={"sides": 20}))
        return await original_complete(
            system_blocks, messages, tools, tool_dispatch, **kwargs
        )

    monkeypatch.setattr(client, "complete_with_tools", _complete_spy)

    # Bypass the real prompt builder — this story tests the SDK seam only.
    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(
        Orchestrator,
        "build_narrator_prompt",
        _fake_build_prompt,
    )

    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=2,
        # Aside-stash grounding (DRIVER verify-fail 2026-06-07): the stash
        # must carry the calendar the get_world_grounding tool would read.
        world_calendar={"starting_date": "0933-04-12"},
    )

    result = await orch.run_narration_turn("look around", ctx)

    # 1. The SDK was driven once — the SDK owns the tool loop (no hand-driven
    #    tool_use → end_turn re-call), so one query() call per turn.
    assert len(fake.calls) == 1

    # 2. The full tool catalog was advertised — Story 119-3 surfaces it as the
    #    SDK-MCP allowed_tools (mcp__narration__<name>), one per registry tool.
    #    Story 54-6 added resolve_location_entity (27th tool); ADR-109 §5.3.
    #    Story 24-6 added get_world_grounding (28th tool); ADR-024 grounding.
    #    Story 59-1 added begin_confrontation (29th tool); SDK engagement writer.
    #    Story 59-4 / ADR-113 RETIRED begin_confrontation (atomic IntentRouter
    #    cutover) — back to 28.
    #    CWN System Strain (#506) added adjust_system_strain (29th tool).
    #    CWN combat lethality (#507) added stabilize_mortal_injury (30th tool).
    #    WWN content wiring (Plan 3) added commit_effort (31st), veterans_luck
    #    (32nd), long_rest (33rd).
    #    Story 77-2 (ADR-137) added record_quest (34th), set_stakes (35th).
    #    AWN Plan 2 (Task 11) added use_mutation (36th tool).
    #    Story 102-5 added wn_attack (37th), wn_skill_check (38th), wn_save
    #    (39th), wn_adjudicate_dead_premise (40th).
    #    ADR-116/144 added propose_fate_compel (41st, ruleset="fate"). 41.
    allowed = fake.last_options.allowed_tools
    assert len(allowed) == len(default_registry.list_names()) == 41
    assert all(name.startswith("mcp__narration__") for name in allowed)

    # 3. The result carries the SDK's text.
    assert result.narration == fake_response_text

    # 4. The narration.turn span has the rollup attributes the GM panel reads.
    narration_spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert len(narration_spans) == 1
    attrs = dict(narration_spans[0].attributes or {})
    assert attrs["narration.turn.model_chosen"] == "claude-sonnet-4-6"
    # Token counts come from the terminal ResultMessage.usage dict.
    assert attrs["narration.turn.total_input_tokens"] == 450
    assert attrs["narration.turn.total_output_tokens"] == 72
    assert attrs["narration.turn.cache_read_tokens"] == 4800
    assert attrs["narration.turn.cache_write_tokens"] == 120
    # The converged stream advertises tools but the SDK never invoked a handler,
    # so the ToolingResult tool-call ledger is empty (spec tool-dispatch limit).
    assert attrs["narration.turn.tool_call_count"] == 0

    # 5. The ToolContext flowing into the registry carries a real
    #    NarratorPerceptionFilter — the perception seam is wired, not None. Driven
    #    via the production tool_dispatch closure (the SDK-MCP bridge's callee).
    assert len(captured_ctx) == 1
    ctx_seen = captured_ctx[0]
    assert isinstance(ctx_seen.perception_filter, NarratorPerceptionFilter)
    assert ctx_seen.perspective_pc == "Kael"
    assert ctx_seen.turn_number == 2

    # 6. Aside-rides-the-cache (playtest 2026-06-07): the SDK turn stashed
    #    its exact prompt artifacts so an out-of-band aside can re-present
    #    the identical prefix. The stash must reference the SAME tool defs
    #    the turn advertised and carry the resolved narration model.
    stash = orch.aside_prompt_stash
    assert stash is not None, "SDK turn must populate the aside prompt stash"
    assert len(stash.tools) == len(default_registry.list_names())
    assert stash.system_blocks, "stash must carry the turn's system blocks"
    assert stash.system_blocks[0].cache is True  # the cached stable prefix
    assert stash.model  # the resolve_model(NARRATION) choice
    # DRIVER verify-fail 2026-06-07: the per-turn game state rides the USER
    # bucket, so the stash must carry the turn's exact user message — and the
    # calendar (tool-only for the narrator) explicitly.
    assert stash.user_state_text == "user text"
    assert '"starting_date": "0933-04-12"' in stash.calendar_summary
    # The handler's path gate: a tooling-backed orchestrator exposes its
    # client for the narrator-cache aside path.
    assert orch.aside_cache_client is client
