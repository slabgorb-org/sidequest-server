"""Wiring test: run_narration_turn → fabricated-roll tripwire (sq-playtest 2026-06-13).

Proves the lie detector is reachable from the public narration entrypoint and
emits the ``narrator.fabricated_roll`` OTEL span (GM-panel signal), and that the
repair is a TOOLLESS rewrite — a second SDK call carrying ``tools=[]`` so it
cannot re-run WRITE tools / double-apply state. Mirrors the fake-SDK shape used
by test_narrator_sdk_hybrid_split.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.fabricated_roll_guard import detect_fabricated_roll
from sidequest.agents.orchestrator import Orchestrator, TurnContext


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


def _text_resp(text: str, model: str = "claude-sonnet-4-6") -> _Resp:
    return _Resp(
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=120, output_tokens=20),
        model=model,
    )


async def _fake_build_prompt(
    self: Orchestrator, action: str, context: TurnContext
) -> tuple[str, Any]:
    class _FakeRegistry:
        def compose_split(self, agent_name: str) -> tuple[str, str]:
            return ("system text", "user text")

        def compose_split_by_zone(self, agent_name: str):
            from sidequest.agents.prompt_framework.types import AttentionZone

            return ({AttentionZone.Primacy: "system text"}, "user text")

        def registry(self, agent_name: str) -> list:
            return []

    return ("prompt-text", _FakeRegistry())


@pytest.mark.asyncio
async def test_fabricated_roll_span_fires_and_prose_is_repaired_toolless(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    clean = "The creature's lunge goes wide — the blow never lands, and you are untouched."
    sdk = _Sdk(
        responses=[
            # Main narration turn: leaks a fabricated die on a server-rolled miss.
            _text_resp("The roll of 3 misses. No damage to you."),
            # Toolless rewrite pass returns laundered prose.
            _text_resp(clean, model="claude-haiku-4-5-20251001"),
        ]
    )
    orch = Orchestrator(client=AnthropicSdkClient(sdk=sdk))
    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    result = await orch.run_narration_turn(
        "strike", TurnContext(character_name="Kael", turn_number=2)
    )

    # 1) The lie-detector span fired with the matched fabrication, marked repaired.
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "narrator.fabricated_roll"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["matched"] == "roll of 3"
    assert attrs["repaired"] is True
    assert attrs["roll_tool_fired"] is False

    # 2) The player-facing prose is laundered — no fabricated number survives.
    assert result.narration == clean
    assert detect_fabricated_roll(result.narration, roll_tool_fired=False) is None

    # 3) The repair was a SECOND SDK call carrying no tools (cannot double-apply).
    assert len(sdk.messages.calls) == 2
    assert sdk.messages.calls[1].get("tools", []) == []


@pytest.mark.asyncio
async def test_clean_narration_fires_no_span_and_no_second_call(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    clean = "The spear scrapes stone; the thing is still up, still close."
    sdk = _Sdk(responses=[_text_resp(clean)])
    orch = Orchestrator(client=AnthropicSdkClient(sdk=sdk))
    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    result = await orch.run_narration_turn(
        "strike", TurnContext(character_name="Kael", turn_number=2)
    )

    assert result.narration == clean
    assert [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.fabricated_roll"
    ] == []
    # No repair call was made — the single scripted response was enough.
    assert len(sdk.messages.calls) == 1
