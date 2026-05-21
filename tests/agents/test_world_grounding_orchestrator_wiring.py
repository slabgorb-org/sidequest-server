"""RED — Story 24-10 AC5/AC6: TurnContext grounding reaches the tool via the orchestrator.

This is the load-bearing wiring test the story context flags as "the single
most load-bearing AC" (24-10 Risk section). It drives the **real**
``Orchestrator.run_narration_turn`` SDK path with a fake Anthropic SDK client
that emits a ``get_world_grounding`` tool call, then lets the **real**
``default_registry.dispatch`` run the actual tool handler. Nothing about the
ToolContext is hand-built: the orchestrator constructs it at
``orchestrator.py:3259`` from the ``TurnContext`` we pass in, exactly as it
does in production. That is the seam Story 24-10 wires (AC5) — three new
kwargs alongside the existing ``lore_store`` / ``monster_manual`` lines.

Two production hops are proven end-to-end here:

* **AC5** — the ToolContext flowing into the registry carries the three
  grounding fields off the TurnContext (capture the dispatched context).
* **AC6** — the real ``get_world_grounding`` handler returns non-null
  weather/demographics/calendar AND fires the ``world_grounding.weather_used``
  + ``world_grounding.demographics_injected`` spans (the GM-panel lie-detector
  signal). With the wiring absent (RED) the ToolContext fields default None,
  the payload sections are null, and neither span fires.

Mirrors the established fake-SDK harness in
``tests/agents/test_narrator_uses_sdk_client.py``. Fixtures only — the
TurnContext grounding is a synthetic ``WeatherState`` + plain dicts; no live
content is read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# Importing the tools package wires the adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock
from sidequest.game.weather import WeatherState

# ---------------------------------------------------------------------------
# Minimal fake SDK (mirrors test_narrator_uses_sdk_client.py)
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
    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")


def _grounding_call_sdk() -> _Sdk:
    """A two-turn SDK: turn 1 calls get_world_grounding (default include =
    all three sections), turn 2 returns final prose."""
    return _Sdk(
        responses=[
            _Resp(
                content=[
                    _ToolUseSdkBlock(
                        type="tool_use",
                        id="toolu_grounding",
                        name="get_world_grounding",
                        input={},
                    )
                ],
                stop_reason="tool_use",
                usage=_Usage(input_tokens=200, output_tokens=24),
                model="claude-sonnet-4-6",
            ),
            _Resp(
                content=[_TextBlock(type="text", text="Haar rolls down the glen.")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=210, output_tokens=30),
                model="claude-sonnet-4-6",
            ),
        ]
    )


def _patch_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)


def _spy_dispatch(monkeypatch: pytest.MonkeyPatch) -> tuple[list, list]:
    """Wrap the REAL dispatch so the actual tool handler runs (spans fire),
    while capturing the ToolContext and ToolResultBlock the orchestrator
    produced."""
    real_dispatch = default_registry.dispatch
    captured_ctx: list[ToolContext] = []
    captured_results: list[ToolResultBlock] = []

    async def _wrapper(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        captured_ctx.append(ctx)
        res = await real_dispatch(block, ctx)
        captured_results.append(res)
        return res

    monkeypatch.setattr(default_registry, "dispatch", _wrapper)
    return captured_ctx, captured_results


_WEATHER = WeatherState(
    zone="glen_floor",
    season="autumn",
    condition="smirr",
    temperature_c=11.5,
    precipitation=True,
    special_event=None,
    effects=[],
    seed=42,
)
_DEMOGRAPHICS = {
    "parish": {"name": "Glenross", "total_population": 412},
    "recurring_cast": [{"id": "minister", "name": "Rev. Aulay"}],
}
_CALENDAR = {"current": {"month": "October", "day": 14, "year": 1908}}


# ---------------------------------------------------------------------------
# AC5 + AC6 — positive wiring path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grounded_turn_context_reaches_tool_context_at_construction_site(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: InMemorySpanExporter,
) -> None:
    """AC5: a TurnContext carrying grounding must reach the ToolContext the
    orchestrator builds at orchestrator.py:3259. With the wiring absent (RED)
    the ToolContext fields default None even though the TurnContext carried
    the data."""
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_prompt(monkeypatch)
    captured_ctx, _ = _spy_dispatch(monkeypatch)

    orch = Orchestrator(client=AnthropicSdkClient(sdk=_grounding_call_sdk()))

    ctx = TurnContext(character_name="Alex", genre="tea_and_murder", turn_number=3)
    # Stamp grounding as the bootstrap → _build_turn_context chain will. setattr
    # (not constructor kwargs) so this test exercises the orchestrator seam
    # regardless of whether the TurnContext field already exists — the
    # dedicated field-existence tripwire lives in the bootstrap suite.
    ctx.weather_state = _WEATHER
    ctx.world_demographics = _DEMOGRAPHICS
    ctx.world_calendar = _CALENDAR

    await orch.run_narration_turn("look at the sky", ctx)

    assert len(captured_ctx) == 1, "get_world_grounding must have been dispatched once"
    tool_ctx = captured_ctx[0]
    assert tool_ctx.weather_state is _WEATHER, (
        "ToolContext.weather_state is None — orchestrator.py:3259 did not thread "
        "context.weather_state through (AC5 wiring missing)"
    )
    assert tool_ctx.world_demographics == _DEMOGRAPHICS
    assert tool_ctx.world_calendar == _CALENDAR


@pytest.mark.asyncio
async def test_grounded_turn_returns_nonnull_payload_and_fires_used_spans(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: InMemorySpanExporter,
) -> None:
    """AC6 (load-bearing): driving a real narration turn whose narrator calls
    get_world_grounding must return non-null weather/demographics/calendar AND
    fire the weather_used + demographics_injected spans. This is the
    end-to-end proof that the wiring is live, not just that components exist."""
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_prompt(monkeypatch)
    _, captured_results = _spy_dispatch(monkeypatch)

    orch = Orchestrator(client=AnthropicSdkClient(sdk=_grounding_call_sdk()))

    ctx = TurnContext(character_name="Alex", genre="tea_and_murder", turn_number=3)
    ctx.weather_state = _WEATHER
    ctx.world_demographics = _DEMOGRAPHICS
    ctx.world_calendar = _CALENDAR

    await orch.run_narration_turn("look at the sky", ctx)

    # Payload non-null (AC6: weather is a dict with WeatherState fields, etc.)
    assert len(captured_results) == 1
    payload = json.loads(captured_results[0].content)
    assert payload["weather"] is not None, "weather section came back null — grounding unwired"
    assert payload["weather"]["zone"] == "glen_floor"
    assert payload["demographics"] is not None
    assert payload["demographics"]["parish"]["total_population"] == 412
    assert payload["calendar"] is not None

    # Spans fired (AC6: weather_used + demographics_injected).
    names = [s.name for s in otel_capture.get_finished_spans()]
    assert "world_grounding.weather_used" in names, (
        "weather_used span did not fire — the narrator's grounding call returned "
        "no weather, so the GM panel cannot tell grounded weather from improvised"
    )
    assert "world_grounding.demographics_injected" in names


# ---------------------------------------------------------------------------
# AC7 — regression guard: an ungrounded turn stays graceful
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ungrounded_turn_returns_null_sections_and_no_spans(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: InMemorySpanExporter,
) -> None:
    """AC7 guard: a turn whose TurnContext carries NO grounding (a pack that
    never authored weather/demographics/calendar) returns three null sections,
    fires neither used nor injected span, and does not crash. The GREEN wiring
    must not break this graceful path."""
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_prompt(monkeypatch)
    _, captured_results = _spy_dispatch(monkeypatch)

    orch = Orchestrator(client=AnthropicSdkClient(sdk=_grounding_call_sdk()))

    # No grounding stamped on the context.
    ctx = TurnContext(character_name="Alex", genre="caverns_and_claudes", turn_number=1)

    result = await orch.run_narration_turn("look around", ctx)
    assert result.narration == "Haar rolls down the glen."

    payload = json.loads(captured_results[0].content)
    assert payload["weather"] is None
    assert payload["demographics"] is None
    assert payload["calendar"] is None

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert "world_grounding.weather_used" not in names
    assert "world_grounding.demographics_injected" not in names
