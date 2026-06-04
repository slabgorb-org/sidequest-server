"""Wiring test for Phase D Task 1 — Orchestrator routes through the SDK + Registry.

When the orchestrator's LlmClient is a ToolingLlmClient (an
AnthropicSdkClient in production), ``run_narration_turn`` must:

* Go through ``AnthropicSdkClient.complete_with_tools``.
* Pass the full 28-tool array from ``default_registry``.
* Open a ``narration.turn`` cost-rollup span and seed the rollup
  attributes (model, token totals, tool-call count).
* Return a ``NarrationTurnResult`` whose ``narration`` field matches the
  SDK's text output.

The test monkeypatches ``build_narrator_prompt`` so it does not have to
exercise the full prompt builder — the seam under test is the SDK
routing path, not prompt assembly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Importing the tools package wires the 28 adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock

# ---------------------------------------------------------------------------
# In-memory fake SDK shaped like the AsyncAnthropic surface we touch.
# Mirrors the pattern in test_anthropic_sdk_client.py / test_anthropic_sdk_client_wiring.py.
# ---------------------------------------------------------------------------


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
class _ToolUseSdkBlock:
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
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


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


@pytest.mark.asyncio
async def test_orchestrator_routes_narration_through_sdk(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: InMemorySpanExporter,
) -> None:
    """When the LlmClient is a ToolingLlmClient, run_narration_turn must
    funnel through complete_with_tools with the full tool catalog and
    populate the narration.turn span rollup attributes.
    """
    # Make sure streaming is disabled — the SDK path is on the sync route.
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    fake_response_text = "The wind rises across the salt flats."
    sdk = _Sdk(
        responses=[
            # First turn — model calls a tool so we can assert the
            # ToolContext flowing into default_registry.dispatch carries a
            # NarratorPerceptionFilter.
            _Resp(
                content=[
                    _ToolUseSdkBlock(
                        type="tool_use",
                        id="toolu_1",
                        name="roll_dice",
                        input={"sides": 20},
                    )
                ],
                stop_reason="tool_use",
                usage=_Usage(
                    input_tokens=200,
                    output_tokens=24,
                    cache_read_input_tokens=2400,
                    cache_creation_input_tokens=120,
                ),
                model="claude-sonnet-4-6",
            ),
            # Second turn — final prose.
            _Resp(
                content=[_TextBlock(type="text", text=fake_response_text)],
                stop_reason="end_turn",
                usage=_Usage(
                    input_tokens=250,
                    output_tokens=48,
                    cache_read_input_tokens=2400,
                    cache_creation_input_tokens=0,
                ),
                model="claude-sonnet-4-6",
            ),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk)
    orch = Orchestrator(client=client)

    # Capture the ToolContext that default_registry.dispatch receives so
    # we can assert NarratorPerceptionFilter and the rest of the wiring
    # reach the registry side of the seam.
    captured_ctx: list[ToolContext] = []

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        captured_ctx.append(ctx)
        # Don't invoke the real registry handler — we don't want to exercise
        # roll_dice's actual side effects. Return a synthetic OK result so
        # the SDK loop completes and rolls forward to the final response.
        return ToolResultBlock(tool_use_id=block.id, content="17", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

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
    )

    result = await orch.run_narration_turn("look around", ctx)

    # 1. The SDK was hit — two iterations (tool_use → end_turn).
    assert len(sdk.messages.calls) == 2

    # 2. The full tool catalog was sent on every iteration.
    sent_tools = sdk.messages.calls[0]["tools"]
    # Story 54-6 added resolve_location_entity (27th tool); ADR-109 §5.3.
    # Story 24-6 added get_world_grounding (28th tool); ADR-024 grounding.
    # Story 59-1 added begin_confrontation (29th tool); SDK engagement writer.
    # Story 59-4 / ADR-113 RETIRED begin_confrontation (atomic IntentRouter
    # cutover) — confrontation engagement moved off the narrator's tool
    # surface and onto the Intent Router pre-narrator pass. Back to 28.
    # CWN System Strain (#506) added adjust_system_strain (29th tool).
    # CWN combat lethality (#507) added stabilize_mortal_injury (30th tool). 30.
    # WWN content wiring (Plan 3) added the narrator-triggered WWN mechanics as
    # WRITE tools mirroring adjust_system_strain: commit_effort (31st),
    # veterans_luck (32nd), long_rest (33rd). 33.
    # Story 77-2 (ADR-137) added typed quest/stakes tools: record_quest (34th),
    # set_stakes (35th). 35.
    assert len(sent_tools) == len(default_registry.list_names()) == 35

    # 3. The result carries the SDK's text.
    assert result.narration == fake_response_text

    # 4. The narration.turn span has the rollup attributes the GM panel reads.
    narration_spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert len(narration_spans) == 1
    attrs = dict(narration_spans[0].attributes or {})
    assert attrs["narration.turn.model_chosen"] == "claude-sonnet-4-6"
    # Token counts are cumulative across the two iterations.
    assert attrs["narration.turn.total_input_tokens"] == 450
    assert attrs["narration.turn.total_output_tokens"] == 72
    assert attrs["narration.turn.cache_read_tokens"] == 4800
    assert attrs["narration.turn.cache_write_tokens"] == 120
    assert attrs["narration.turn.tool_call_count"] == 1

    # 5. The ToolContext flowing into the registry carries a real
    #    NarratorPerceptionFilter — the perception seam is wired, not None.
    assert len(captured_ctx) == 1
    ctx_seen = captured_ctx[0]
    assert isinstance(ctx_seen.perception_filter, NarratorPerceptionFilter)
    assert ctx_seen.perspective_pc == "Kael"
    assert ctx_seen.turn_number == 2


@pytest.mark.asyncio
async def test_tooling_client_takes_sdk_path_even_when_streaming_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: SIDEQUEST_NARRATOR_STREAMING=1 must NOT route a
    ToolingLlmClient into the streaming path.

    Before the fix, ``run_narration_turn`` gated the SDK path on
    ``not is_streaming_enabled()``. With streaming on, the default
    ``AnthropicSdkClient`` (a ``ToolingLlmClient``) fell through to
    ``_run_narration_turn_streaming``, which asserts the client is *not* a
    ToolingLlmClient — crashing every turn with AssertionError
    ("streaming path must not see a ToolingLlmClient, got
    AnthropicSdkClient") and a dead, narration-less turn for the player.
    SDK streaming is Phase D Task 7 (unimplemented), so a tooling client
    must always take the SDK path until then, regardless of the flag.
    """
    monkeypatch.setenv("SIDEQUEST_NARRATOR_STREAMING", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client = AnthropicSdkClient(sdk=_Sdk(responses=[]))
    orch = Orchestrator(client=client)

    routed: list[str] = []

    async def _spy_sdk(
        self: Orchestrator,
        action: str,
        context: TurnContext,
        *,
        room: object | None = None,
    ) -> str:
        # Story 71-23: _run_narration_turn_sdk now accepts room for delta fan-out.
        routed.append("sdk")
        return "sentinel-sdk-result"

    async def _spy_streaming(
        self: Orchestrator,
        action: str,
        context: TurnContext,
        *,
        room: object | None = None,
    ) -> str:
        routed.append("streaming")
        return "sentinel-streaming-result"

    monkeypatch.setattr(Orchestrator, "_run_narration_turn_sdk", _spy_sdk)
    monkeypatch.setattr(Orchestrator, "_run_narration_turn_streaming", _spy_streaming)

    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=1,
    )

    result = await orch.run_narration_turn("look around", ctx)

    # The tooling client took the SDK path; the streaming path (which would
    # have raised AssertionError on the real method) was never entered.
    assert routed == ["sdk"], (
        f"ToolingLlmClient must take the SDK path even with "
        f"SIDEQUEST_NARRATOR_STREAMING=1; routed={routed}"
    )
    assert result == "sentinel-sdk-result"
