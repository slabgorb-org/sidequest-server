"""Story 119-3 — narrator behaviours preserved across the agent-sdk port.

The transport rewrite (raw ``anthropic`` Messages SDK → ``claude-agent-sdk``
``query()``) preserved four narrator behaviours whose former coverage lived in
the deleted raw-SDK integration suites (per-iteration ``messages.create`` loop,
cache markers). This file re-pins them on the new transport, driven by the fake
``query`` seam (OQ-9):

* the multi-text-block discard span (five_points doubled-narration fix, §7.3);
* the soft ``iteration_cap`` cap-hit span (story 71-40);
* the ``loop_exceeded`` summary span on the ``max_turns`` raise path (story 82-9);
* the cost-runaway detector + per-session ceiling firing through
  ``complete_with_tools`` (the $313-incident backstop — the detector/ceiling
  *logic* is unit-tested transport-free in ``test_cost_safety_unit``; this proves
  the ported client still *feeds* it).
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.agents.tooling_protocol import CacheableBlock, Message
from tests.agents.fakes.fake_agent_sdk import (
    FakeAssistantMessage,
    FakeQuery,
    FakeResultMessage,
    FakeTextBlock,
    converged_text_stream,
    error_result_stream,
    fake_usage,
)

_SONNET = "claude-sonnet-4-6"


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture
def fresh_ledger():
    from sidequest.agents import cost_safety

    cost_safety.ledger().reset_for_tests()
    yield cost_safety.ledger()
    cost_safety.ledger().reset_for_tests()


def _new_client() -> Any:
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    return AnthropicSdkClient()


async def _drive(client: Any, **kw: Any) -> Any:
    return await client.complete_with_tools(
        [CacheableBlock(text="rules", cache=True)],
        [Message(role="user", content="look around")],
        [],
        None,
        model=_SONNET,
        **kw,
    )


async def test_multi_text_block_discard_span_fires(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any
) -> None:
    """An assistant message with >1 text block keeps the LAST telling and emits
    the discard audit span (never a silent trim)."""
    from sidequest.agents import anthropic_sdk_client

    stream = [
        FakeAssistantMessage(
            content=[FakeTextBlock(text="draft telling"), FakeTextBlock(text="final telling")]
        ),
        FakeResultMessage(result="final telling", num_turns=2, usage=fake_usage()),
    ]
    monkeypatch.setattr(anthropic_sdk_client, "query", FakeQuery(stream), raising=False)

    result = await _drive(_new_client())

    assert result.text == "final telling", "the last text block is the converged telling"
    discard = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "narrator.multi_text_block_discarded"
    ]
    assert discard, "the discard must be audited with a span, not silently trimmed"
    assert dict(discard[-1].attributes or {}).get("discarded_count") == 1


async def test_iteration_cap_hit_span_fires(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any
) -> None:
    """A turn whose num_turns reaches the soft iteration_cap records ONE cap-hit
    span (a throttle warning, not a stop)."""
    from sidequest.agents import anthropic_sdk_client

    fake = FakeQuery(converged_text_stream(text="ok", num_turns=5))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client(), iteration_cap=3)

    cap_hits = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.tool_loop.cap_hit"
    ]
    assert cap_hits, "reaching the soft iteration_cap must emit a cap-hit span"
    assert dict(cap_hits[-1].attributes or {}).get("iteration_cap") == 3


async def test_max_turns_emits_loop_exceeded_span_then_raises(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any
) -> None:
    """The worst-latency turn (max_turns hit) still emits the tool_loop summary
    span marked loop_exceeded before the fail-loud raise (story 82-9)."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkLoopExceeded

    fake = FakeQuery(error_result_stream(subtype="error_max_turns"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AnthropicSdkLoopExceeded):
        await _drive(_new_client())

    loop_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.tool_loop"
    ]
    assert loop_spans, "a ceiling-blown turn must still emit the summary span"
    assert dict(loop_spans[-1].attributes or {}).get("loop_exceeded") is True


async def test_cost_runaway_detector_fires_through_client(
    monkeypatch: pytest.MonkeyPatch, fresh_ledger: Any
) -> None:
    """The ported complete_with_tools still feeds the runaway detector: a
    60K-in/12-out turn on a session trips cost_runaway_suspected."""
    from sidequest.agents import anthropic_sdk_client, cost_safety

    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        cost_safety,
        "_watcher_publish_event",
        lambda name, fields, **_kw: events.append((name, fields)),
    )

    fake = FakeQuery(
        converged_text_stream(
            text="ok", usage=fake_usage(input_tokens=60_000, output_tokens=12)
        )
    )
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    await _drive(_new_client(), session_id="runaway-1")

    runaway = [f for n, f in events if n == "cost_runaway_suspected"]
    assert runaway, "the io_fingerprint runaway must fire through the ported client"
    assert runaway[0]["trigger"] == "io_fingerprint"
    assert runaway[0]["caller"] == "narrator"


async def test_session_ceiling_pre_flight_refuses_through_client(
    monkeypatch: pytest.MonkeyPatch, fresh_ledger: Any
) -> None:
    """A session already over its ceiling is refused at the pre-flight check
    before any spend (the terminal-refusal contract), even on the new transport."""
    from sidequest.agents import anthropic_sdk_client
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkCostCeilingExceeded

    fresh_ledger.cumulative_cost_usd["killed"] = 999.0  # well past the $10 default
    fake = FakeQuery(converged_text_stream(text="never reached"))
    monkeypatch.setattr(anthropic_sdk_client, "query", fake, raising=False)

    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await _drive(_new_client(), session_id="killed")
    assert not fake.calls, "a ceiling-killed session must not reach the SDK"
