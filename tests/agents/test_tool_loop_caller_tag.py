"""Story 82-9 (RED) — curate calls must not pollute narrator solo-turn p95 (AC3).

71-40's ``narrator.tool_loop`` summary span fires on EVERY converged
``complete_with_tools`` call — including the non-narrator dungeon **curate** stage
(``dungeon/materializer.py:1208``). Those curate loops emit a span named
``narrator.tool_loop`` with no caller discriminator, contaminating AC3's
solo-turn p95 source and mislabeling curate calls on the GM panel (Reviewer
Improvement finding).

This story tags the summary span with a ``caller`` discriminator so the two are
separable post-hoc: the narrator turn path tags ``caller="narrator"`` and the
curate path tags a distinct non-narrator caller. The GM panel filters
solo-turn p95 to ``caller="narrator"``.

Coverage:
* a REAL narrator turn (``Orchestrator.run_narration_turn``) tags the span
  ``caller="narrator"`` — genuine call-site wiring, not a unit of the client;
* the curate-signature call (the exact kwargs materializer:1208 uses) tags a
  distinct caller and is distinguishable from the narrator value.

RED: the summary span carries no ``caller`` attribute, so curate and narrator
loops are indistinguishable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.agents.tools  # noqa: F401 — wires the tool adapters onto default_registry
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)

_SUMMARY_SPAN = "narrator.tool_loop"


# --- minimal SDK-shape fakes ------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: Any = None


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


def _text_resp(text: str = "The salt flats stretch on.") -> _Resp:
    return _Resp(
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=_Usage(input_tokens=20, output_tokens=8),
        model="claude-sonnet-4-6",
    )


class _FakeRegistry:
    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        return []


def _spans_named(otel_capture, name: str) -> list[dict]:
    return [dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == name]


# --- AC3a: the real narrator turn path tags caller="narrator" ----------------


@pytest.mark.asyncio
async def test_narrator_turn_tags_tool_loop_caller_narrator(
    monkeypatch: pytest.MonkeyPatch,
    otel_capture: InMemorySpanExporter,
) -> None:
    """Driving the REAL ``run_narration_turn`` (the production narrator call
    site) must tag the ``narrator.tool_loop`` summary span ``caller="narrator"``
    — the value the GM panel filters solo-turn p95 to."""
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sdk = _Sdk(responses=[_text_resp()])
    orch = Orchestrator(client=AnthropicSdkClient(sdk=sdk))

    async def _fake_build_prompt(self: Orchestrator, action: str, context: TurnContext):
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(character_name="Kael", genre="caverns_and_claudes", turn_number=2)
    await orch.run_narration_turn("look around", ctx)

    summary = _spans_named(otel_capture, _SUMMARY_SPAN)
    assert len(summary) == 1, f"one tool_loop summary span per turn; got {len(summary)}"
    assert summary[0].get("caller") == "narrator", (
        "the narrator turn path must tag the tool_loop span caller='narrator' so "
        f"the GM panel can isolate solo-turn p95; got {summary[0]}"
    )


# --- AC3b: the curate-signature call tags a distinct, non-narrator caller -----


def _curate_dispatch(block: ToolUseBlock) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)


@pytest.mark.asyncio
async def test_curate_signature_call_tags_distinct_caller(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """The dungeon-curate call (materializer:1208 — ``tools=[]``,
    ``tool_dispatch=None``, no ``session_id``, ``caller="dungeon_curate"``) must
    tag the summary span with that caller, distinct from ``"narrator"`` — so
    curate loops are filtered OUT of solo-turn p95."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicSdkClient(sdk=_Sdk(responses=[_text_resp("curated.")]))

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="curate rules", cache=False)],
        messages=[Message(role="user", content="curate this region")],
        tools=[],
        tool_dispatch=None,
        model="claude-sonnet-4-6",
        caller="dungeon_curate",
    )

    summary = _spans_named(otel_capture, _SUMMARY_SPAN)
    assert len(summary) == 1
    assert summary[0].get("caller") == "dungeon_curate", (
        f"a curate-signature call must tag caller='dungeon_curate'; got {summary[0]}"
    )
    assert summary[0].get("caller") != "narrator", (
        "curate must be distinguishable from narrator on the tool_loop span — "
        "that separability is the whole point of AC3"
    )


@pytest.mark.asyncio
async def test_tool_loop_caller_defaults_to_narrator(
    monkeypatch: pytest.MonkeyPatch, otel_capture
) -> None:
    """Back-compat: a call that does not pass ``caller`` defaults to
    ``"narrator"`` — preserving the 71-40 contract that the bare summary span is
    a narrator solo-turn (the existing tool-loop tests rely on this)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicSdkClient(sdk=_Sdk(responses=[_text_resp()]))

    await client.complete_with_tools(
        system_blocks=[CacheableBlock(text="rules", cache=True)],
        messages=[Message(role="user", content="look")],
        tools=[],
        model="claude-sonnet-4-6",
    )

    summary = _spans_named(otel_capture, _SUMMARY_SPAN)
    assert len(summary) == 1
    assert summary[0].get("caller") == "narrator", (
        f"absent an explicit caller, the summary span defaults to narrator; got {summary[0]}"
    )
