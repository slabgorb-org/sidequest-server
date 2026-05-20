"""Cache-TTL cost fix: prerequisite prefix-stability gate + OTEL wiring.

Two guarantees the 1h ephemeral-cache restore depends on:

1. **Prerequisite gate.** The cached system prefix — ``compose_split(agent
   name)[0]``, wrapped verbatim into the single cached ``CacheableBlock``
   at ``Orchestrator._run_narration_turn_sdk`` — MUST be byte-identical
   across sequential turns of one fixed game. A 1h cache write on a
   *mutating* prefix is 2x the base write cost every turn — strictly
   worse than no cache. If this test fails, the fix is invalid: do not
   flip the operative default to 1h.

2. **OTEL wiring.** The ``narration.turn`` cost span must carry
   ``narration.turn.cache_ttl`` so the GM panel can compute write
   amortization (paired with the already-emitted
   ``narration.turn.cache_write_tokens``) and prove the fix engaged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Importing the tools package wires the 26 adapters onto default_registry,
# matching the production SDK path's expectations.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.orchestrator import Orchestrator
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)


def _end_turn(text: str) -> ScriptedResponse:
    return ScriptedResponse(
        text=text,
        stop_reason="end_turn",
        input_tokens=120,
        output_tokens=18,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
    )


@pytest.mark.asyncio
async def test_compose_split_system_prefix_byte_identical_across_3_turns(
    simple_turn_context,
) -> None:
    """PREREQUISITE GATE — the cached prefix does not move turn-to-turn.

    Drives three sequential narration turns through the real prompt
    builder + SDK path with only turn-dynamic inputs changing (action
    text + turn number). The cached block (recorded ``system_blocks[0]``)
    must hash to a single value across all three turns.
    """
    fake = FakeAnthropicSdkClient(
        responses=[_end_turn("turn one"), _end_turn("turn two"), _end_turn("turn three")]
    )
    orch = Orchestrator(client=fake)

    for n in range(3):
        ctx = replace(simple_turn_context, turn_number=n)
        await orch.run_narration_turn(f"player does distinct thing number {n}", ctx)

    assert len(fake.recorded_requests) == 3, (
        f"expected one recorded SDK request per turn; got {len(fake.recorded_requests)}"
    )
    prefixes = [r.system_blocks[0].text for r in fake.recorded_requests]
    digests = {hashlib.sha256(p.encode("utf-8")).hexdigest() for p in prefixes}
    assert len(digests) == 1, (
        "cached system prefix MUST be byte-identical across turns of one "
        f"fixed game; got {len(digests)} distinct prefixes — a 1h cache "
        "write on a mutating prefix is worse than no cache. DO NOT flip "
        "the operative default to 1h until this holds."
    )


@pytest.mark.asyncio
async def test_sdk_path_emits_zone_aligned_cacheable_blocks(
    simple_turn_context,
) -> None:
    """Phase D Task 6 — system_blocks split by attention zone.

    The orchestrator should ship one cache=True block (Primacy + Early
    scaffolding) and at most two uncached follow-on blocks for Valley
    and Late+Recency content. Only the first block is cache-marked;
    Anthropic's cache_control semantics treat the first marker as the
    prefix end-point, and uncached blocks past that point may mutate
    per turn without invalidating the cache.
    """
    fake = FakeAnthropicSdkClient(responses=[_end_turn("turn one")])
    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", simple_turn_context)

    assert len(fake.recorded_requests) == 1
    blocks = fake.recorded_requests[0].system_blocks
    assert len(blocks) >= 1, "must always ship at least one system block"
    assert blocks[0].cache is True, (
        f"the first block carries the cache marker; got cache={blocks[0].cache}"
    )
    assert blocks[0].text, "the cached prefix must not be empty"
    for i, block in enumerate(blocks[1:], start=1):
        assert block.cache is False, (
            f"only system_blocks[0] is cache-marked; "
            f"system_blocks[{i}].cache should be False, got True"
        )


# --- OTEL wiring: narration.turn.cache_ttl ---------------------------------


@dataclass
class _CacheCreation:
    ephemeral_5m_input_tokens: int = 0
    ephemeral_1h_input_tokens: int = 0


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: _CacheCreation | None = None


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


@pytest.mark.asyncio
async def test_narration_turn_span_carries_cache_ttl(
    simple_turn_context,
    otel_capture: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narration.turn cost span exposes the configured cache TTL so
    the GM panel can prove the 1h fix engaged and compute amortization."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(
        responses=[
            _Resp(
                content=[_TextBlock(type="text", text="The torch sputters.")],
                stop_reason="end_turn",
                usage=_Usage(
                    input_tokens=300,
                    output_tokens=40,
                    cache_read_input_tokens=28000,
                    cache_creation_input_tokens=0,
                ),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")
    orch = Orchestrator(client=client)

    await orch.run_narration_turn("look around", simple_turn_context)

    turn_spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert turn_spans, "expected a narration.turn span"
    attrs = dict(turn_spans[0].attributes or {})
    assert attrs.get("narration.turn.cache_ttl") == "1h", (
        f"narration.turn.cache_ttl should reflect the client's configured "
        f"TTL; got {attrs.get('narration.turn.cache_ttl')!r}"
    )


@pytest.mark.asyncio
async def test_narration_turn_span_carries_total_cost_usd(
    simple_turn_context,
    otel_capture: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Task B1 — narration.turn.total_cost_usd is populated from the
    SDK client's cumulative cost. Also covers Task B3 — the SDK client
    emits a `narrator.sdk.usage` log line per iteration so cache numbers
    show up in /tmp/sidequest-server.log without needing a WS tap.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(
        responses=[
            _Resp(
                content=[_TextBlock(type="text", text="The torch sputters.")],
                stop_reason="end_turn",
                usage=_Usage(
                    input_tokens=500,
                    output_tokens=80,
                    cache_read_input_tokens=12000,
                    cache_creation_input_tokens=0,
                ),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")
    orch = Orchestrator(client=client)

    import logging

    with caplog.at_level(logging.INFO, logger="sidequest.agents.anthropic_sdk_client"):
        await orch.run_narration_turn("look around", simple_turn_context)

    turn_spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert turn_spans, "expected a narration.turn span"
    attrs = dict(turn_spans[0].attributes or {})
    cost = attrs.get("narration.turn.total_cost_usd")
    assert isinstance(cost, float) and cost > 0.0, (
        f"narration.turn.total_cost_usd should be a positive float when "
        f"tokens were consumed; got {cost!r}"
    )

    # Task B3 — per-iter usage line includes cache numbers and cost.
    usage_lines = [r.message for r in caplog.records if "narrator.sdk.usage" in r.message]
    assert usage_lines, (
        "expected at least one `narrator.sdk.usage` log line per turn"
    )
    line = usage_lines[0]
    for needle in ("iter=1", "input=500", "output=80", "cache_read=12000", "cost_usd="):
        assert needle in line, f"missing {needle!r} in usage line: {line!r}"


@pytest.mark.asyncio
async def test_narration_turn_span_carries_ttl_breakdown(
    simple_turn_context,
    otel_capture: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """narration.turn span exposes 5m vs 1h write breakdown so the GM
    panel can verify the tools-cache fix engaged."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(
        responses=[
            _Resp(
                content=[_TextBlock(type="text", text="The torch sputters.")],
                stop_reason="end_turn",
                usage=_Usage(
                    input_tokens=300,
                    output_tokens=40,
                    cache_read_input_tokens=10000,
                    cache_creation_input_tokens=15000,
                    cache_creation=_CacheCreation(
                        ephemeral_5m_input_tokens=0,
                        ephemeral_1h_input_tokens=15000,
                    ),
                ),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")
    orch = Orchestrator(client=client)

    await orch.run_narration_turn("look around", simple_turn_context)

    turn_spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert turn_spans, "expected a narration.turn span"
    attrs = dict(turn_spans[0].attributes or {})
    assert attrs.get("narration.turn.cache_write_5m_tokens") == 0, (
        f"expected 0; got {attrs.get('narration.turn.cache_write_5m_tokens')!r}"
    )
    assert attrs.get("narration.turn.cache_write_1h_tokens") == 15000, (
        f"expected 15000; got {attrs.get('narration.turn.cache_write_1h_tokens')!r}"
    )


@pytest.mark.asyncio
async def test_narration_turn_span_carries_system_block_sizes_json(
    simple_turn_context,
    otel_capture: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stability-audit diagnostic — span carries per-block token sizes
    so drift in 'stable' zones surfaces in the GM panel."""
    import json as _json

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(
        responses=[
            _Resp(
                content=[_TextBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            )
        ]
    )
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")
    orch = Orchestrator(client=client)

    await orch.run_narration_turn("look around", simple_turn_context)

    turn_spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert turn_spans, "expected a narration.turn span"
    attrs = dict(turn_spans[0].attributes or {})
    raw = attrs.get("narration.turn.system_block_sizes_json")
    assert isinstance(raw, str), f"expected JSON string; got {type(raw).__name__}"
    sizes = _json.loads(raw)
    # Required keys — all four regions must report a size even if zero.
    assert set(sizes.keys()) == {"stable", "valley", "recency", "tools"}, (
        f"unexpected key set: {sorted(sizes.keys())}"
    )
    # Each size is a non-negative int (token estimate via char-count / 4).
    for name, value in sizes.items():
        assert isinstance(value, int) and value >= 0, (
            f"{name}={value!r} must be a non-negative int"
        )
    # Stable region must be non-empty on a real narration turn.
    assert sizes["stable"] > 0, "stable region must carry content"


def test_tool_definitions_json_byte_identical_across_calls() -> None:
    """Tools-region cache marker (added 2026-05-20) only buys 1h caching if
    the serialized tools array is byte-identical across calls. This regression
    test asserts that — if it ever fails, the tools cache will silently re-mint
    every turn even with the marker present."""
    from sidequest.agents.tool_registry import default_registry

    snapshots: list[str] = []
    for _ in range(3):
        payload = json.dumps(
            [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in default_registry.tool_definitions()
            ]
        )
        snapshots.append(payload)

    assert snapshots[0] == snapshots[1] == snapshots[2], (
        "tool_definitions() JSON drifted across calls — tools cache will re-mint"
    )
