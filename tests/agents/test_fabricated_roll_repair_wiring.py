"""Wiring test: run_narration_turn → fabricated-roll tripwire (sq-playtest 2026-06-13).

Proves the lie detector is reachable from the public narration entrypoint and
emits the ``narrator.fabricated_roll`` OTEL span (GM-panel signal), and that the
repair is a TOOLLESS rewrite — a second SDK call carrying ``tools=[]`` so it
cannot re-run WRITE tools / double-apply state.

Story 119-3: the transport is ``claude-agent-sdk``. Both ``complete_with_tools``
calls (the main turn + the toolless repair rewrite) drive the late-bound
``anthropic_sdk_client.query`` seam. The fabricated-roll detector/repair logic is
unchanged; only the transport the orchestrator drives changed. The repair's
``tools=[]`` surfaces as an empty ``allowed_tools`` on the second call's
``ClaudeAgentOptions`` (no SDK-MCP server advertised).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents import anthropic_sdk_client
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.fabricated_roll_guard import detect_fabricated_roll
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from tests.agents.fakes.fake_agent_sdk import converged_text_stream


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


class _SequencedFakeQuery:
    """A fake ``query`` that replays a DIFFERENT scripted stream per call.

    The orchestrator drives ``query`` once per ``complete_with_tools`` call: the
    main narration turn, then (when a fabricated roll is detected) the toolless
    repair rewrite. ``FakeQuery`` replays one fixed stream on every call, so this
    sequenced variant pops the next stream per ``__call__`` — the cheatsheet's
    "fake that returns different streams" shape.
    """

    def __init__(self, streams: list[list[Any]]) -> None:
        self._streams = list(streams)
        self.calls: list[SimpleNamespace] = []

    def __call__(self, *, prompt: Any, options: Any) -> AsyncIterator[Any]:
        self.calls.append(SimpleNamespace(prompt=prompt, options=options))
        stream = self._streams.pop(0)
        return self._aiter(stream)

    async def _aiter(self, stream: list[Any]) -> AsyncIterator[Any]:
        for msg in stream:
            yield msg


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
    clean = "The creature's lunge goes wide — the blow never lands, and you are untouched."
    fake = _SequencedFakeQuery(
        [
            # Main narration turn: leaks a fabricated die on a server-rolled miss.
            converged_text_stream(text="The roll of 3 misses. No damage to you."),
            # Toolless rewrite pass returns laundered prose.
            converged_text_stream(text=clean),
        ]
    )
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)
    orch = Orchestrator(client=AnthropicSdkClient())
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

    # 3) The repair was a SECOND SDK call carrying no tools (cannot double-apply):
    #    tools=[] surfaces as an empty allowed_tools / no SDK-MCP server on the
    #    second call's ClaudeAgentOptions.
    assert len(fake.calls) == 2
    assert fake.calls[1].options.allowed_tools == []
    assert fake.calls[1].options.mcp_servers == {}


@pytest.mark.asyncio
async def test_clean_narration_fires_no_span_and_no_second_call(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    clean = "The spear scrapes stone; the thing is still up, still close."
    fake = _SequencedFakeQuery([converged_text_stream(text=clean)])
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)
    orch = Orchestrator(client=AnthropicSdkClient())
    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    result = await orch.run_narration_turn(
        "strike", TurnContext(character_name="Kael", turn_number=2)
    )

    assert result.narration == clean
    assert [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.fabricated_roll"
    ] == []
    # No repair call was made — the single scripted response was enough.
    assert len(fake.calls) == 1
