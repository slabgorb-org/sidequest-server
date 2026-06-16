"""Story 119-3 RED — AC3: narrator tool-loop port fidelity.

The narrator's ``complete_with_tools`` body is rewritten from the manual
``messages.create`` + ``stop_reason=='tool_use'`` re-call loop onto the Agent
SDK's own ``query()`` loop (spec §5, §7). The ``ToolingLlmClient`` signature,
the ``ToolingResult`` dataclass, the 26-tool registry, and every OTEL span are
preserved. This file pins the preserved boundary:

* convergence → a populated ``ToolingResult`` (spec §7.2);
* ``max_turns`` → ``AnthropicSdkLoopExceeded`` (the fail-loud convergence
  contract, §7.5);
* the toolless path converges (fabricated-roll repair / aside, §8.1);
* OTEL preserved — ``narrator.tool_loop`` (caller + iterations_used) and the
  ``narrator.sdk.usage`` watcher event (§7.4);
* the ``@tool`` → ``default_registry.dispatch`` bridge re-enters dispatch with
  the **bare** tool name (§5.3);
* the ported method is reachable from the orchestrator (the mandated wiring
  test, server CLAUDE.md).

All tests drive the fake ``query`` seam (OQ-9) — no live subscription.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

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
    error_result_stream,
)

_SONNET = "claude-sonnet-4-6"


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _new_client() -> Any:
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    return AnthropicSdkClient()


async def _drive(client: Any, *, tools: list[ToolDefinition] | None = None) -> Any:
    return await client.complete_with_tools(
        [CacheableBlock(text="rules", cache=True)],
        [Message(role="user", content="look around")],
        tools if tools is not None else [
            ToolDefinition(name="roll_dice", description="Roll", input_schema={"type": "object"})
        ],
        None,
        model=_SONNET,
    )


# ===========================================================================
# Convergence + ToolingResult contract
# ===========================================================================


async def test_tool_loop_converges_returns_tooling_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.agents import anthropic_sdk_client

    prose = "The strike lands; the bandit reels."
    fake = FakeQuery(converged_text_stream(text=prose, num_turns=3))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    result = await _drive(_new_client())

    assert result.text == prose, "converged narration prose must be the result text"
    assert result.tool_calls == [], "a no-tool turn must carry an empty tool_calls ledger"
    assert result.model == _SONNET, "the resolved model id must survive onto the result"
    assert result.stop_reason in {"end_turn", "stop_sequence", "max_tokens"}, (
        f"a converged success must map to a non-error stop_reason; got {result.stop_reason!r}"
    )
    # Token fields are preserved as ints (semantics, §4 fidelity rule) — exact
    # values depend on the usage-dict adaptation, a GREEN concern (OQ-2/OQ-6).
    assert isinstance(result.input_tokens, int)
    assert isinstance(result.output_tokens, int)
    assert isinstance(result.cumulative_cost_usd, float)


async def test_max_turns_raises_loop_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``max_turns`` hit (terminal ``error_max_turns`` ResultMessage) must
    raise ``AnthropicSdkLoopExceeded`` — the preserved fail-loud convergence
    contract, not a silent truncated narration."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkLoopExceeded

    fake = FakeQuery(error_result_stream(subtype="error_max_turns"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AnthropicSdkLoopExceeded):
        await _drive(_new_client())


async def test_toolless_path_converges(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tools=[]`` (the fabricated-roll rewrite / aside path, §8.1) converges
    in one turn with no tool surface advertised."""
    from sidequest.agents import anthropic_sdk_client

    prose = "You arrive in Montmartre. It's raining."
    fake = FakeQuery(converged_text_stream(text=prose))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    result = await _drive(_new_client(), tools=[])
    assert result.text == prose
    assert result.tool_calls == []


# ===========================================================================
# OTEL preservation (spec §7.4)
# ===========================================================================


async def test_narrator_tool_loop_span_and_usage_event_preserved(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any
) -> None:
    """``narrator.tool_loop`` (carrying ``caller='narrator'`` and
    ``iterations_used == num_turns``) and the ``narrator.sdk.usage`` watcher
    event must still fire on the ported path."""
    from sidequest.agents import anthropic_sdk_client

    events: list[tuple[str, dict[str, Any]]] = []

    def _record(event_type: str, fields: dict[str, Any], **_kw: Any) -> None:
        events.append((event_type, fields))

    monkeypatch.setattr(anthropic_sdk_client, "_watcher_publish_event", _record)

    fake = FakeQuery(converged_text_stream(text="The lantern gutters.", num_turns=4))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client())

    loop_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.tool_loop"
    ]
    assert loop_spans, "the per-turn narrator.tool_loop summary span must still fire"
    attrs = dict(loop_spans[-1].attributes or {})
    assert attrs.get("caller") == "narrator", "tool_loop span must carry caller=narrator"
    assert attrs.get("iterations_used") == 4, (
        "iterations_used must equal ResultMessage.num_turns (spec §7.2 mapping); "
        f"got {attrs.get('iterations_used')!r}"
    )

    assert any(name == "narrator.sdk.usage" for name, _ in events), (
        "the narrator.sdk.usage watcher event must still fire on the ported "
        f"transport (GM-panel cost baseline); events seen: {[n for n, _ in events]!r}"
    )


# ===========================================================================
# The @tool → registry.dispatch bridge (spec §5.3)
# ===========================================================================


async def test_tool_bridge_dispatches_bare_name_and_accumulates() -> None:
    """The per-turn ``@tool`` handler bridge must re-enter ``tool_dispatch``
    with a ``ToolUseBlock`` carrying the **bare** tool name + the model's args
    (the registry only knows bare names), append it to the tool-call ledger,
    and return the SDK ``{"content":[...], "is_error":...}`` shape so the SDK
    feeds the result back to the model.

    Pinned as a unit on the bridge factory: the SDK-owned loop calls our
    handler, and the SDK-MCP server introspection (OQ-1) is unverified, so the
    bridge is tested at its callable boundary rather than by driving the live
    SDK loop. (TEA deviation — see session notes.)"""
    from sidequest.agents.anthropic_sdk_client import _build_narration_tool_handler

    seen: list[ToolUseBlock] = []

    async def _dispatch(block: ToolUseBlock) -> ToolResultBlock:
        seen.append(block)
        return ToolResultBlock(tool_use_id=block.id, content="17", is_error=False)

    accumulator: list[ToolUseBlock] = []
    handler = _build_narration_tool_handler(
        bare_name="roll_dice",
        tool_dispatch=_dispatch,
        accumulator=accumulator,
    )

    sdk_result = await handler({"sides": 20})

    assert len(seen) == 1, "the bridge must call tool_dispatch exactly once"
    assert seen[0].name == "roll_dice", (
        "dispatch must receive the BARE tool name, not the mcp__server__name "
        f"namespaced id; got {seen[0].name!r}"
    )
    assert seen[0].arguments == {"sides": 20}, "the model's args must reach dispatch verbatim"

    assert [b.name for b in accumulator] == ["roll_dice"], (
        "every tool call must be accumulated so ToolingResult.tool_calls is "
        "complete (the fabricated-roll detector + GM-panel ledger depend on it)"
    )

    assert sdk_result.get("is_error") is False
    content = sdk_result.get("content")
    assert isinstance(content, list) and content, "SDK handler reply must carry a content list"
    assert content[0].get("type") == "text"
    assert content[0].get("text") == "17", (
        "the ToolResultBlock content must flow back to the SDK as the handler "
        f"reply; got {content!r}"
    )


# ===========================================================================
# Wiring — reachable from the orchestrator (server CLAUDE.md)
# ===========================================================================


@dataclass
class _FakeRegistry:
    """Minimum PromptRegistry surface ``_run_narration_turn_sdk`` reads."""

    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str) -> tuple[dict[Any, str], str]:
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list[Any]:
        return []


async def test_complete_with_tools_reachable_from_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture-driven wiring test: the orchestrator's ``run_narration_turn``
    must funnel through the ported ``complete_with_tools`` (driven by the
    late-bound ``query`` seam) and surface the converged narration — proving
    the port is wired, not unit-tested in isolation."""
    import sidequest.agents.tools  # noqa: F401 — wires the registry
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    prose = "The wind rises across the salt flats."
    fake = FakeQuery(converged_text_stream(text=prose))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    client = anthropic_sdk_client.AnthropicSdkClient()
    orch = Orchestrator(client=client)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=2,
        world_calendar={"starting_date": "0933-04-12"},
    )

    result = await orch.run_narration_turn("look around", ctx)

    assert result.narration == prose, (
        "the orchestrator must surface the ported transport's converged "
        f"narration; got {result.narration!r}"
    )
    assert fake.calls, "the ported complete_with_tools must have driven the query seam"
