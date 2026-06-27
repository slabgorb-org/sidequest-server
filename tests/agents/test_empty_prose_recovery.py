"""PLAYTEST (caverns_and_claudes/beneath_sunden, WWN, 2026-06-27, region seam):

On the turn crossing from the authored `entrance` into a freshly edge-expanded
maze-maker region, the SDK narrator fired WRITE tools (set the location + visual
scene) but produced ZERO player-facing prose — the "tool-only response." The
`_guard_empty_narration` symptom net recovered the client (no hang) by substituting
the generic in-fiction stall "The world holds its breath.", and story 153-11 made
the CAUSE loud (`narrator.empty_prose_upstream cause=tool_only_response`). But the
player still got generic filler at the seam instead of arrival narration.

This is the reprompt-for-prose recovery story 153-11 explicitly DEFERRED ("This
story does NOT add a reprompt-for-prose recovery ... see Delivery Findings for that
design fork"). When the narrator ACTED (fired tools) but narrated nothing, do ONE
TOOLLESS reprompt (tools=[] — cannot re-run WRITE tools / double-apply state) that
asks for the player-facing narration of the moment it just set. Real narrator prose
beats the generic stall. The degraded-stall guard REMAINS the fallback when the
reprompt also comes back empty (or the turn had no tools to narrate from).

SDK-path-only (the toolless reprompt is a tool-loop concept); the synchronous path
keeps the downstream guard alone.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

# Importing the tools package wires the 26 adapters onto default_registry,
# matching the production SDK path's expectations.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.claude_client import ClaudeResponse
from sidequest.agents.orchestrator import NarrationTurnResult, Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

RECOVERED_SPAN = "narrator.empty_prose_recovered"
STALL = "world holds its breath"

# A realistic WRITE tool the narrator fires while setting the new region's scene.
_LOCATION_TOOL = ToolUseBlock(
    id="toolu_loc_1",
    name="resolve_location_entity",
    arguments={"label": "The Shaft Bottom", "kind": "narrator_proactive"},
)


def _response(
    *,
    text: str,
    stop_reason: str,
    tool_uses: list[ToolUseBlock] | None = None,
) -> ScriptedResponse:
    return ScriptedResponse(
        text=text,
        stop_reason=stop_reason,
        input_tokens=200,
        output_tokens=12,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
        tool_uses=tool_uses or [],
    )


class _FakeRegistry:
    """Minimal PromptRegistry stand-in (mirrors test_153_11_empty_prose_upstream)."""

    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        return []


async def _drive_sdk_turn(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeAnthropicSdkClient,
    *,
    action: str = "My boots hit stone. I step off the rope and look around.",
) -> NarrationTurnResult:
    """Run the action through the real ``run_narration_turn`` SDK path with ``client``."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    orch = Orchestrator(client=client)

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(self, action, context):
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(character_name="Curly", genre="caverns_and_claudes", turn_number=2)
    return await orch.run_narration_turn(action, ctx)


# ---------------------------------------------------------------------------
# The recovery: tools fired + empty prose → ONE toolless reprompt yields the
# arrival narration; the turn is NOT degraded and shows real prose, not the stall.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_only_empty_prose_recovers_via_reprompt(monkeypatch, otel_capture):
    client = FakeAnthropicSdkClient(
        responses=[
            _response(text="", stop_reason="tool_use", tool_uses=[_LOCATION_TOOL]),
            _response(text="", stop_reason="end_turn"),  # the tool-only empty turn
            # the toolless recovery reprompt yields the arrival prose:
            _response(
                text="Your boots strike cold stone. The shaft opens into a low chamber, "
                "the dark pressing close past the torchlight.",
                stop_reason="end_turn",
            ),
        ]
    )

    result = await _drive_sdk_turn(monkeypatch, client)

    assert result.is_degraded is False, "a recovered turn is real prose, not a degraded stall"
    assert STALL not in result.narration.lower(), "recovered prose must replace the generic stall"
    assert "stone" in result.narration.lower(), "the recovered arrival narration is surfaced"
    recovered = [s for s in otel_capture.get_finished_spans() if s.name == RECOVERED_SPAN]
    assert len(recovered) == 1, "a recovery attempt fires the narrator.empty_prose_recovered span"
    assert dict(recovered[0].attributes or {}).get("recovered") is True


# ---------------------------------------------------------------------------
# Fallback: the reprompt ALSO comes back empty → the degraded-stall guard still
# recovers the client (153-11 AC1 — the guard remains the last resort).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reprompt_also_empty_falls_back_to_degraded_stall(monkeypatch):
    client = FakeAnthropicSdkClient(
        responses=[
            _response(text="", stop_reason="tool_use", tool_uses=[_LOCATION_TOOL]),
            _response(text="", stop_reason="end_turn"),
            _response(text="   \n  ", stop_reason="end_turn"),  # recovery whitespace-only
        ]
    )

    result = await _drive_sdk_turn(monkeypatch, client)

    assert result.is_degraded is True, "a failed recovery must still trip the degraded stall"
    assert STALL in result.narration.lower()


# ---------------------------------------------------------------------------
# Scope: a no-tools empty turn (no_output) has nothing acted to narrate from —
# no reprompt is attempted; the guard handles it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tools_empty_does_not_reprompt(monkeypatch, otel_capture):
    # Only ONE scripted response — if the recovery wrongly reprompted, the fake
    # would raise ScriptExhausted; instead the guard handles it from the single turn.
    client = FakeAnthropicSdkClient(responses=[_response(text="", stop_reason="end_turn")])

    result = await _drive_sdk_turn(monkeypatch, client)

    assert result.is_degraded is True
    assert STALL in result.narration.lower()
    assert not [s for s in otel_capture.get_finished_spans() if s.name == RECOVERED_SPAN], (
        "no_output (no tools) must NOT attempt a prose reprompt — nothing was acted to narrate"
    )


# ---------------------------------------------------------------------------
# Negative: a healthy SDK turn (real prose) never reprompts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_turn_does_not_reprompt(monkeypatch, otel_capture):
    client = FakeAnthropicSdkClient(
        responses=[_response(text="The torch gutters as you descend.", stop_reason="end_turn")]
    )

    result = await _drive_sdk_turn(monkeypatch, client)

    assert result.is_degraded is False
    assert result.narration == "The torch gutters as you descend."
    assert not [s for s in otel_capture.get_finished_spans() if s.name == RECOVERED_SPAN]


# ---------------------------------------------------------------------------
# Scope: the synchronous (non-tooling) path never reprompts — no tool-use loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synchronous_empty_turn_does_not_reprompt(simple_turn_context, otel_capture):
    client = AsyncMock()
    client.send_stateless = AsyncMock(return_value=ClaudeResponse(text="", session_id=None))
    orch = Orchestrator(client=client)

    result = await orch.run_narration_turn("look around", simple_turn_context)

    assert result.is_degraded is True  # downstream guard handles it
    assert STALL in result.narration.lower()
    assert not [s for s in otel_capture.get_finished_spans() if s.name == RECOVERED_SPAN]
