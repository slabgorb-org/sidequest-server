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

UPDATE (Story 60-4, 2026-05-23): the moving 1h cache_control breakpoint on
the tool-loop continuation has landed in ``complete_with_tools``, so the
rebate now materializes on continuation calls (measured: prefix write moves
from ephemeral_5m to ephemeral_1h, next identical continuation reads it at
write=0). These tests remain the stability + wiring gate they always were;
the realized-rebate behavior is now covered by
``tests/agents/test_60_4_continuation_cache_breakpoint.py``. See
``sprint/archive/60-3-session.md`` (diagnosis) and
``sprint/archive/60-4-session.md`` (fix).
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
    assert usage_lines, "expected at least one `narrator.sdk.usage` log line per turn"
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
        assert isinstance(value, int) and value >= 0, f"{name}={value!r} must be a non-negative int"
    # Stable region must be non-empty on a real narration turn.
    assert sizes["stable"] > 0, "stable region must carry content"


# --- ADR-112 / Story 57-3: promoted genre prose rides the cached System block --


def _prompts_with_all_promotions() -> Any:
    """Construct an in-memory ``Prompts`` model with every Story-57-3-
    promoted section populated by a marker string the tests can grep for.

    Inline construction avoids depending on a specific genre pack's
    ``prompts.yaml`` content being loaded into the test environment.
    Required ``narrator``/``combat``/``npc``/``world_state`` fields are
    given placeholder text; the four originally-promoted optional fields
    carry the assertable marker strings.

    Story 61-11 (ADR-112 amendment): ``genre_chargen`` was demoted from
    STABLE and now gates on ``TurnContext.opening_directive is not None``.
    The marker stays in this fixture so tests can assert either presence
    (predicate true) or absence (predicate false) without rebuilding
    the Prompts shape; the dedicated chargen tests below cover both
    states.
    """
    from sidequest.genre.models.narrative import Prompts

    return Prompts(
        narrator="(test) narrator voice",
        combat="(test) combat voice",
        npc="(test) npc voice",
        world_state="(test) world state",
        extraction="(test) extraction prose — STORY_57_3_EXTRACTION",
        keeper_monologue="(test) keeper monologue prose — STORY_57_3_KEEPER",
        town="(test) town prose — STORY_57_3_TOWN",
        chargen="(test) chargen prose — STORY_57_3_CHARGEN",
    )


@pytest.mark.asyncio
async def test_promoted_genre_prose_lands_in_cached_system_block(
    simple_turn_context,
) -> None:
    """ADR-112 / Story 57-3 — the still-promoted genre prose sections ride
    the cached ``system_blocks[0]`` block, not the per-turn user message
    or any uncached follow-on system block.

    This is the direct cache evidence path (see Story 57-3 brief, 2026-05-20):
    the per-turn ``narration.turn.system_block_sizes_json["stable"]``
    attribute is expected to grow by the sections' combined size on every
    turn after promotion.

    Pre-promotion the sections registered at ``orchestrator.py`` with
    ``AttentionZone.Valley`` and ``SectionBucket.User``, so their content
    (wrapped in ``<genre-extraction>``, ``<genre-keeper>``,
    ``<genre-town>`` tags) landed in the per-turn user message — uncached.

    Post-promotion (allowlist contains the section names) the bucket
    classifier returns ``SectionBucket.System`` for each, so the marker
    tags MUST appear in the cached block —
    ``recorded_requests[0].system_blocks[0].text`` — and MUST NOT appear
    in any user message content.

    Story 61-11 (ADR-112 amendment): ``genre_chargen`` is no longer
    asserted in this loop — it was demoted from STABLE and gated on
    ``TurnContext.opening_directive is not None``. With the
    ``simple_turn_context`` fixture (``opening_directive=None``), the
    chargen section is not registered at all this turn. The dedicated
    ``test_demoted_genre_chargen_*`` tests below cover both predicate
    states for chargen.

    Hand-built ``Prompts`` is injected on the ``TurnContext.genre_prompts``
    slot so the registration sites at ``orchestrator.py`` fire
    deterministically without relying on a specific pack's
    ``prompts.yaml`` being loaded by the test environment.
    """
    ctx = replace(simple_turn_context, genre_prompts=_prompts_with_all_promotions())

    fake = FakeAnthropicSdkClient(responses=[_end_turn("ok")])
    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", ctx)

    assert len(fake.recorded_requests) == 1, (
        f"expected exactly one recorded SDK request; got {len(fake.recorded_requests)}"
    )
    request = fake.recorded_requests[0]

    cached_block_text = request.system_blocks[0].text
    other_system_text = "\n".join(b.text for b in request.system_blocks[1:])
    user_message_text = "\n".join(m.content for m in request.messages if m.role == "user")

    # Story 61-11: chargen removed from this loop — gated separately below.
    promoted_markers = (
        "<genre-extraction>",
        "<genre-keeper>",
        "<genre-town>",
    )
    for marker in promoted_markers:
        assert marker in cached_block_text, (
            f"{marker!r} did not land in the cached system_blocks[0].text. "
            f"ADR-112 promotion routes these sections into the Stable "
            f"(cached) block; if the marker is missing here, the cache "
            f"savings ADR-112 promises are not materializing — likely the "
            f"section is still registered against AttentionZone.Valley and "
            f"the zone-aligned cache split (orchestrator.py:3112-3135) is "
            f"keeping it out of system_blocks[0]. "
            f"Found instead in system_blocks[1:]: {marker in other_system_text}; "
            f"found in user message: {marker in user_message_text}."
        )
        assert marker not in user_message_text, (
            f"{marker!r} still appears in the per-turn user message — "
            f"the User→System bucket migration did not take effect for "
            f"this section. Check STABLE_SECTION_NAMES in "
            f"prompt_framework/bucket.py."
        )
        assert marker not in other_system_text, (
            f"{marker!r} landed in an uncached system block, not the "
            f"cached system_blocks[0]. ADR-112 §Consequences:Positive "
            f"requires the content to be cached for the amortization "
            f"claim to hold."
        )

    # Story 61-11 regression guard: chargen is gated on
    # ``opening_directive is not None``. The ``simple_turn_context``
    # fixture has ``opening_directive=None``, so the marker must be
    # absent from EVERY part of the prompt this turn.
    full_prompt_text = cached_block_text + "\n" + other_system_text + "\n" + user_message_text
    assert "<genre-chargen>" not in full_prompt_text, (
        "<genre-chargen> appeared somewhere in the prompt on a turn "
        "with opening_directive=None. Story 61-11 demoted chargen and "
        "gated it on TurnContext.opening_directive — this turn should "
        "skip registration entirely."
    )


@pytest.mark.asyncio
async def test_demoted_genre_chargen_lands_in_user_message_on_opening_turn(
    simple_turn_context,
) -> None:
    """Story 61-11 (ADR-112 amendment) — ``genre_chargen`` was demoted from
    STABLE and now gates on ``TurnContext.opening_directive is not None``.

    On the post-chargen opening turn the gate passes and registration
    fires; the section routes to the User bucket (uncached) because it
    was dropped from ``STABLE_SECTION_NAMES``. Both halves are
    load-bearing:

      - If the gate is broken (registers when predicate is false), the
        prose returns on every neutral turn — the per-turn carry the
        story is eliminating.
      - If the bucket assignment is broken (section is registered but
        ends up back in System), the cache root carries the prose on
        every turn that the gate fires.

    This test pins the predicate-true / demoted-bucket combination
    against the SDK request shape, mirroring the same assertion path
    as ``test_promoted_genre_prose_lands_in_cached_system_block`` but
    inverted for the demoted section.
    """
    ctx = replace(
        simple_turn_context,
        genre_prompts=_prompts_with_all_promotions(),
        opening_directive="ESTABLISHING: torchlight, dripping water, the descent begins.",
    )

    fake = FakeAnthropicSdkClient(responses=[_end_turn("ok")])
    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", ctx)

    request = fake.recorded_requests[0]
    cached_block_text = request.system_blocks[0].text
    other_system_text = "\n".join(b.text for b in request.system_blocks[1:])
    user_message_text = "\n".join(m.content for m in request.messages if m.role == "user")

    assert "<genre-chargen>" in user_message_text, (
        "<genre-chargen> did not land in the user message on the opening "
        "turn (opening_directive set). Expected the post-demotion path: "
        "predicate gate fires → bucket assignment routes to User → "
        "content appears in the per-turn user message. Check the "
        "registration block at orchestrator.py (chargen, ~line 1595)."
    )
    assert "<genre-chargen>" not in cached_block_text, (
        "<genre-chargen> landed in the CACHED system_blocks[0] on the "
        "opening turn. Story 61-11 demoted chargen out of "
        "STABLE_SECTION_NAMES; it must route to the User bucket "
        "(uncached) regardless of whether the predicate fires. If it "
        "is back in the cached block, ``genre_chargen`` was likely "
        "re-added to STABLE_SECTION_NAMES — revert that change."
    )
    assert "<genre-chargen>" not in other_system_text, (
        "<genre-chargen> landed in an uncached system block on the "
        "opening turn. The demoted section should route to the User "
        "bucket only; appearance in any system block indicates the "
        "bucket classifier still maps it to System."
    )


@pytest.mark.asyncio
async def test_deferred_combat_voice_does_not_ride_cached_system_block(
    simple_turn_context,
) -> None:
    """ADR-112 §Defer regression guard — even if ``genre_combat_voice``
    is conditionally registered (here: turn-0 peace, NOT registered),
    a future author who unconditionally registers it AND promotes it
    must not land it in the cached block. This pairs with the
    bucket-classifier unit test and catches the "promoted AND made
    unconditional in the same change" failure mode.

    On a turn-0 peace context the ``<genre-combat>`` and ``<genre-chase>``
    marker tags must not appear in *any* part of the prompt — they
    aren't registered. If they appear in ``system_blocks[0]``, the
    Defer rationale (cache thrash at every combat boundary) has been
    silently violated.
    """
    ctx = replace(simple_turn_context, genre_prompts=_prompts_with_all_promotions())

    fake = FakeAnthropicSdkClient(responses=[_end_turn("ok")])
    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", ctx)

    request = fake.recorded_requests[0]
    full_prompt = "\n".join(
        [b.text for b in request.system_blocks]
        + [m.content for m in request.messages if m.role == "user"]
    )

    for marker in ("<genre-combat>", "<genre-chase>"):
        assert marker not in full_prompt, (
            f"{marker!r} appeared in the prompt on a turn-0 peace context. "
            f"The conditional registration guards at "
            f"orchestrator.py:1344..1356 must continue to gate these "
            f"sections on context.in_combat / context.in_chase — and the "
            f"bucket classifier must continue to route them to "
            f"SectionBucket.User if/when they do register (ADR-112 §Defer)."
        )


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
