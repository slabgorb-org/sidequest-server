"""Story 61-19 — Stop 1h-cache-writing the per-turn volatile block.

RED-phase gate. Live forensics on session 894 (perseus_cloud, 17 turns,
claude-sonnet-4-6) show cache_write is 73% of session cost: a ~9.7k-token
VOLATILE block (slimmed snapshot + monster_manual + lore/recency, plus the
player action) is written into the **1h** cache tier on the first iter of
every turn, then invalidated next turn after a single read. The 1h tier's
2x write premium ($6/M on Sonnet) only pays off when content persists and is
re-read across turns; volatile content has zero cross-turn value, so 1h is
strictly worse than 5m (1.25x) or plain uncached input ($3/M).

MECHANISM (verified 2026-05-30 against live code):
  - ``orchestrator.py`` builds ``system_blocks = [stable(cache=True),
    valley(cache=False), recency(cache=False)]`` — only the stable
    identity/voice/SOUL prefix is cacheable; valley/recency carry no
    system-array marker (correct).
  - ``AnthropicSdkClient._build_messages_payload`` (Story 60-7) stamps the
    NEWEST user message's last content block with
    ``cache_control={'type':'ephemeral','ttl': self.cache_ttl}`` — **1h** by
    default. Anthropic prefix-caching writes everything between the previous
    breakpoint (end of the stable system block) and this marker at the
    marker's TTL — so the 1h user-message marker pulls the volatile
    valley+recency+user tail into the 1h write tier every turn.
  - ``complete_with_tools`` (Story 60-4) stamps the continuation
    tool_result message at ``self.cache_ttl`` too — so even iter=2 re-writes
    the volatile tail at 1h.

THE FIX (mechanism is an Architect/Dev call — see context-story-61-19.md):
  Keep the stable system prefix at 1h (it amortizes across turns); move the
  VOLATILE tail (user-message marker + continuation marker) off 1h to the
  5m tier (covers the seconds-long within-turn tool loop at 1.25x, never
  pays the 2x cross-turn premium). Option (a) "send it uncached" is the
  alternative; these tests assert the load-bearing invariant (NOT 1h) plus
  the story-preferred 5m resolution, and are explicit where the two options
  diverge.

SPEC CONFLICT WITH STORY 60-7 (see Delivery Findings + Design Deviations):
  ``test_60_7_iter1_cache_marker.py`` asserts the volatile user-message tail
  IS marked 1h. 61-19 supersedes that tier choice. The green phase MUST flip
  the TTL-VALUE assertions in those 60-7 tests (the marker-EXISTS, 4-cap, and
  stale-cleanup assertions stay valid — only the hardcoded '1h' on the
  *message* markers becomes '5m'). Named precisely in the session Delivery
  Findings.

Wiring discipline (server CLAUDE.md "No Source-Text Wiring Tests"): every
integration test drives a synthetic SDK response through the real
``complete_with_tools`` path and inspects the captured wire payload or the
``watcher_hub`` transport boundary. No test greps source.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub

# --- session-894 measured profile (the regression these tests defend) ------

VOLATILE_TAIL_TOKENS = 9_753  # ~9.7k written at 1h every first-iter, pre-fix
SONNET = "claude-sonnet-4-6"
ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
FIVE_MIN = {"type": "ephemeral", "ttl": "5m"}

# AC5 field contract this story establishes on the per-turn pulse.
STABLE_PREFIX_WRITE_FIELD = "stable_prefix_write_tokens"
TAIL_WRITE_FIELD = "tail_write_tokens"


# --- SDK-shape fakes (mirror tests/agents/test_60_7_iter1_cache_marker.py) --


@dataclass(frozen=True)
class _CacheCreation:
    ephemeral_5m_input_tokens: int = 0
    ephemeral_1h_input_tokens: int = 0


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: _CacheCreation | None = None


@dataclass(frozen=True)
class _TextBlock:
    type: str
    text: str


@dataclass(frozen=True)
class _ToolUseBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class _Resp:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _Msgs:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeSdk: out of scripted responses")
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


def _end_turn(
    text: str = "ok",
    *,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
) -> _Resp:
    return _Resp(
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=_Usage(
            input_tokens=50,
            output_tokens=4,
            cache_read_input_tokens=cache_read,
            cache_creation=_CacheCreation(
                ephemeral_5m_input_tokens=cache_write_5m,
                ephemeral_1h_input_tokens=cache_write_1h,
            ),
        ),
        model=SONNET,
    )


def _tool_use(
    tool_id: str,
    name: str = "roll_dice",
    *,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
) -> _Resp:
    return _Resp(
        content=[_ToolUseBlock(type="tool_use", id=tool_id, name=name, input={})],
        stop_reason="tool_use",
        usage=_Usage(
            input_tokens=50,
            output_tokens=4,
            cache_creation=_CacheCreation(
                ephemeral_5m_input_tokens=cache_write_5m,
                ephemeral_1h_input_tokens=cache_write_1h,
            ),
        ),
        model=SONNET,
    )


def _dispatch_seventeen(block: ToolUseBlock) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=block.id, content="17", is_error=False)


def _tools_one() -> list[ToolDefinition]:
    return [ToolDefinition(name="roll_dice", description="Roll", input_schema={"type": "object"})]


def _three_zone_system() -> list[CacheableBlock]:
    """The production three-zone layout: stable prefix (cache=True) +
    volatile valley + volatile recency (both cache=False)."""
    return [
        CacheableBlock(text="IDENTITY/VOICE/SOUL (stable across turns)", cache=True),
        CacheableBlock(text="VALLEY: slimmed snapshot + monster_manual (volatile)", cache=False),
        CacheableBlock(text="RECENCY: lore + recent beats (volatile)", cache=False),
    ]


class _FakeSocket:
    """watcher_hub subscriber collecting every published event, so tests
    assert delivery to the GM-panel transport — not just a logger call.
    Same pattern as the 60-7 / 61-4 tests."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the watcher hub to the test loop and clear subscribers.
    Mirrors tests/agents/test_60_7_iter1_cache_marker.py::bound_hub."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


def _message_markers(call: dict[str, Any]) -> list[dict[str, Any]]:
    """Every cache_control marker on message-level content blocks in a call."""
    markers: list[dict[str, Any]] = []
    for msg in call.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                markers.append(block["cache_control"])
    return markers


def _system_markers(call: dict[str, Any]) -> list[dict[str, Any]]:
    """Every cache_control marker on system-array blocks in a call."""
    return [
        block["cache_control"]
        for block in call.get("system", [])
        if isinstance(block, dict) and "cache_control" in block
    ]


def _split_events(sock: _FakeSocket) -> list[dict[str, Any]]:
    """Per-turn watcher events carrying the AC5 stable/tail write split.
    Event NAME is left to the implementer (the existing
    ``session.cost_running_total`` per-turn pulse is the natural home — see
    'Don't Reinvent'); the test filters by the field contract so it is
    robust to the chosen event_type."""
    return [
        e
        for e in sock.events
        if isinstance(e.get("fields"), dict)
        and STABLE_PREFIX_WRITE_FIELD in e["fields"]
        and TAIL_WRITE_FIELD in e["fields"]
    ]


# ===========================================================================
# AC4 — the volatile tail must NOT be written to the 1h tier (load-bearing)
# ===========================================================================


def test_volatile_user_message_tail_not_marked_1h_on_1h_client() -> None:
    """AC4 (unit, load-bearing invariant). On a 1h-configured client, the
    newest user message's last content block MUST NOT carry a 1h
    cache_control marker. The user message is volatile (the player's action
    changes every turn); 1h's 2x cross-turn premium is pure waste on it.

    This DIRECTLY inverts 60-7's
    ``test_build_messages_payload_marks_iter1_user_message_at_1h``. 61-19
    supersedes that tier choice — see module docstring + Delivery Findings.

    Allows either the story-preferred 5m tier OR option-(a) uncached
    (marker absent); asserts only the invariant 'not 1h'.
    """
    client = AnthropicSdkClient(sdk=_Sdk(responses=[]), cache_ttl="1h")
    running_messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "I draw my blade"}]}
    ]

    payload = client._build_messages_payload(running_messages, is_continuation=False)  # noqa: SLF001

    last_block = payload[-1]["content"][-1]
    assert isinstance(last_block, dict), f"last block must be a dict; got {last_block!r}"
    assert last_block.get("cache_control") != ONE_HOUR, (
        "The volatile user-message tail MUST NOT be written at the 1h tier "
        "(2x premium on content that dies next turn). 60-7 marked it 1h; "
        "61-19 supersedes that. Expected 5m or no marker. "
        f"Got cache_control={last_block.get('cache_control')!r}."
    )


def test_volatile_user_message_tail_uses_5m_tier_on_1h_client() -> None:
    """AC4 (unit, story-preferred resolution). The volatile tail SHOULD ride
    the 5m tier: a marker still exists (preserving 60-7's single-write /
    within-turn-reuse property for the seconds-long tool loop) but at 1.25x,
    not 2x.

    If the Architect picks option (a) 'uncached' instead, this test is
    retargeted at spec-check (the marker would be absent). The story names
    5m as preferred via the read-count justification (the tail is read ~once,
    within the turn — 5m covers that; 1h's cross-turn persistence is moot).
    """
    client = AnthropicSdkClient(sdk=_Sdk(responses=[]), cache_ttl="1h")
    running_messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "I draw my blade"}]}
    ]

    payload = client._build_messages_payload(running_messages, is_continuation=False)  # noqa: SLF001

    last_block = payload[-1]["content"][-1]
    assert last_block.get("cache_control") == FIVE_MIN, (
        "The volatile tail SHOULD ride the 5m tier (1.25x) so the within-turn "
        "tool loop still reads from cache while never paying the 1h 2x "
        "cross-turn premium. Even on a 1h-configured client the VOLATILE "
        "marker must resolve to 5m (the stable prefix keeps 1h). "
        f"Got cache_control={last_block.get('cache_control')!r}."
    )


def test_stable_system_prefix_keeps_1h_volatile_system_blocks_unmarked() -> None:
    """AC4 (regression guard). The fix must NOT demote the amortizing stable
    prefix. ``_build_system_array`` on the production three-zone layout MUST
    keep block 0 (the cache=True stable prefix) at 1h, and the volatile
    valley/recency blocks (cache=False) carry NO system-array marker.
    """
    client = AnthropicSdkClient(sdk=_Sdk(responses=[]), cache_ttl="1h")

    system = client._build_system_array(_three_zone_system())  # noqa: SLF001

    assert system[0].get("cache_control") == ONE_HOUR, (
        "The stable system prefix (block 0, cache=True) MUST stay on the 1h "
        "tier — it is byte-stable across turns and amortizes. "
        f"Got {system[0].get('cache_control')!r}."
    )
    for i, block in enumerate(system[1:], start=1):
        assert "cache_control" not in block, (
            f"Volatile system block {i} (cache=False) must carry no "
            f"system-array marker; got {block.get('cache_control')!r}."
        )


@pytest.mark.asyncio
async def test_no_volatile_content_under_1h_breakpoint_in_assembled_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 + AC4 (integration). Drive a real single-iter turn through
    ``complete_with_tools`` with the production three-zone system layout, and
    assert the ONLY 1h cache_control markers in the wire request sit on the
    system array (the stable prefix) and/or tools — NEVER on a message-level
    block. A 1h marker on any message block writes the volatile tail at 1h,
    which is exactly the ~9.7k/turn regression this story kills.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_end_turn("done")])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=_three_zone_system(),
        messages=[Message(role="user", content="I search the room")],
        tools=_tools_one(),
        model=SONNET,
        session_id="61-19-no-volatile-1h",
    )

    assert len(sdk.messages.calls) == 1, "expected a single end_turn call"
    call = sdk.messages.calls[0]
    msg_markers = _message_markers(call)
    assert ONE_HOUR not in msg_markers, (
        "No MESSAGE-level block may carry a 1h cache_control marker — the "
        "volatile valley/recency/user tail sits between the stable-prefix "
        "breakpoint and the message breakpoint, so a 1h message marker "
        "writes ~9.7k volatile tokens at the 2x tier every turn. "
        f"Got message markers={msg_markers!r}."
    )
    # The stable prefix SHOULD still be 1h — the amortizing half stays.
    assert ONE_HOUR in _system_markers(call), (
        "The stable system prefix must keep its 1h marker (it amortizes "
        f"across turns). System markers={_system_markers(call)!r}."
    )


@pytest.mark.asyncio
async def test_continuation_marker_does_not_rewrite_volatile_tail_at_1h(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4 (integration, the 60-4 reconciliation + read-count justification).
    In a 2-iter tool loop, the iter=2 continuation marker (60-4) on the
    newest tool_result message MUST NOT be 1h either — otherwise the volatile
    tail is re-written at 1h on continuation, recreating the waste on the
    second SDK call of every turn.

    This is the read-count justification in executable form: the volatile
    tail is written on iter=1 and read on the continuation within the SAME
    turn (seconds apart). 5m covers that window. 1h's cross-turn persistence
    is never used — the tail is gone next turn — so paying the 2x premium on
    BOTH the iter=1 and iter=2 markers is pure loss.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_tool_use("toolu_1"), _end_turn("done")])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=_three_zone_system(),
        messages=[Message(role="user", content="roll it")],
        tools=_tools_one(),
        tool_dispatch=_dispatch_seventeen,
        model=SONNET,
        session_id="61-19-continuation-tier",
    )

    assert len(sdk.messages.calls) == 2, (
        f"expected iter-1 tool_use then iter-2 end_turn; got {len(sdk.messages.calls)}"
    )
    continuation_markers = _message_markers(sdk.messages.calls[1])
    assert ONE_HOUR not in continuation_markers, (
        "The 60-4 continuation marker on the volatile tail MUST move off 1h "
        "too (to 5m), else the tail is 1h-written on the second SDK call of "
        f"every turn. Got continuation message markers={continuation_markers!r}."
    )


# ===========================================================================
# AC5 — per-turn OTEL span splits cache_write into stable-prefix vs tail
# ===========================================================================


@pytest.mark.asyncio
async def test_per_turn_event_exposes_stable_prefix_and_tail_write_split(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC5 (payload contract). A turn MUST publish a per-turn watcher event
    carrying ``stable_prefix_write_tokens`` and ``tail_write_tokens`` so the
    GM panel can see churn regressions: a healthy session writes the stable
    prefix once (warmup) then near-zero, while the tail write recurs per
    turn but small. Under the fix the split maps onto the TTL tiers
    (stable=1h-write, tail=5m-write), and the two MUST sum to the turn's
    total cache_write.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # Warm turn: stable prefix is a READ (write_1h=0), volatile tail is a
    # fresh 5m write of ~1k (the post-fix steady state).
    sdk = _Sdk(responses=[_end_turn("done", cache_write_5m=1_024, cache_write_1h=0, cache_read=25_000)])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    await client.complete_with_tools(
        system_blocks=_three_zone_system(),
        messages=[Message(role="user", content="I listen at the door")],
        tools=[],
        model=SONNET,
        session_id="61-19-split-contract",
    )
    await asyncio.sleep(0.05)

    events = _split_events(sock)
    assert len(events) == 1, (
        "Exactly one per-turn event carrying the stable/tail cache_write "
        f"split must reach watcher subscribers; got {len(events)}. All event "
        f"types: {[e.get('event_type') for e in sock.events]}."
    )
    fields = events[0]["fields"]
    stable = fields[STABLE_PREFIX_WRITE_FIELD]
    tail = fields[TAIL_WRITE_FIELD]
    assert isinstance(stable, int) and stable >= 0, fields
    assert isinstance(tail, int) and tail >= 0, fields
    assert tail == 1_024, (
        "tail_write_tokens must report the volatile tail's per-turn write "
        f"(the 5m write, 1024); got {tail}."
    )
    assert stable == 0, (
        "On a warm turn the stable prefix is read, not written; "
        f"stable_prefix_write_tokens must be 0, got {stable}."
    )
    assert fields.get("total_write_tokens") == stable + tail, (
        "total_write_tokens must equal stable_prefix_write_tokens + "
        f"tail_write_tokens ({stable} + {tail}); got "
        f"{fields.get('total_write_tokens')} — the GM panel's churn total "
        "must stay internally consistent with its two legs."
    )


@pytest.mark.asyncio
async def test_per_turn_split_event_is_info_severity_on_narrator_sdk_component(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC5 (mandatory wiring test). The split event, captured at the
    watcher_hub transport boundary (proving real wiring from the production
    ``complete_with_tools`` per-turn path), MUST be ``severity='info'``
    (a routine baseline pulse, not an alarm) and ``component='narrator.sdk'``
    (groups with the 60-7 / followup-B siblings in the Subsystems tab).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_end_turn("done", cache_write_5m=1_024, cache_write_1h=0)])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")
    await client.complete_with_tools(
        system_blocks=_three_zone_system(),
        messages=[Message(role="user", content="go")],
        tools=[],
        model=SONNET,
        session_id="61-19-split-wiring",
    )
    await asyncio.sleep(0.05)

    events = _split_events(sock)
    assert len(events) == 1, f"need exactly one split event; got {len(events)}"
    assert events[0].get("severity") == "info", (
        "The per-turn cache-write split is a continuous baseline — severity "
        f"MUST be 'info', not an alarm level. Got {events[0].get('severity')!r}."
    )
    assert events[0].get("component") == "narrator.sdk", (
        "Component must be 'narrator.sdk' to group with the sibling cache "
        f"events. Got {events[0].get('component')!r}."
    )


@pytest.mark.asyncio
async def test_per_turn_split_fires_once_per_turn_not_per_iter(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC5 (cadence). AC5 specifies a per-TURN span. A two-iteration tool
    loop MUST emit exactly ONE split event (aggregating both iters' writes),
    NOT one per SDK call — distinguishing it from the per-iter
    ``narrator.sdk.usage`` baseline. The aggregated tail write MUST be the
    sum of the per-iter 5m writes.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # iter=1 writes the tail (5m=900); iter=2 continuation writes a small
    # delta (5m=120). Stable prefix already cached (1h write = 0 both iters).
    sdk = _Sdk(
        responses=[
            _tool_use("toolu_1", cache_write_5m=900, cache_write_1h=0),
            _end_turn("done", cache_write_5m=120, cache_write_1h=0),
        ]
    )
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")
    await client.complete_with_tools(
        system_blocks=_three_zone_system(),
        messages=[Message(role="user", content="roll")],
        tools=_tools_one(),
        tool_dispatch=_dispatch_seventeen,
        model=SONNET,
        session_id="61-19-split-cadence",
    )
    await asyncio.sleep(0.05)

    events = _split_events(sock)
    assert len(events) == 1, (
        "The cache-write split is a per-TURN span — a 2-iter turn must emit "
        f"exactly one (aggregated), not one per SDK call. Got {len(events)}."
    )
    assert events[0]["fields"][TAIL_WRITE_FIELD] == 1_020, (
        "Per-turn tail_write_tokens must aggregate both iters' 5m writes "
        f"(900 + 120 = 1020); got {events[0]['fields'][TAIL_WRITE_FIELD]}."
    )
    assert events[0]["fields"][STABLE_PREFIX_WRITE_FIELD] == 0, (
        "Both iters reported cache_write_1h=0, so the aggregated "
        "stable_prefix_write_tokens must be 0 — a non-zero value means the "
        "1h/5m aggregation buckets are crossed. Got "
        f"{events[0]['fields'][STABLE_PREFIX_WRITE_FIELD]}."
    )


# ===========================================================================
# AC2 / AC3 — economic-model guards (the absolute thresholds are
# integration/playtest-validated — see Delivery Findings).
# ===========================================================================


def test_moving_volatile_write_off_1h_reduces_per_turn_cost() -> None:
    """AC2 (economic-direction guard). Pin the pricing premise the whole
    story rests on: writing the ~9.7k volatile tail at 1h costs strictly more
    than at 5m, which costs strictly more than sending it uncached. If a
    future pricing-table change ever inverts this, the story's premise (and
    fix direction) is invalid and this guard fails loudly.

    NOTE: this guards the cost MODEL/direction. AC2's absolute
    '<=$0.05/turn at steady state on a 17+ turn session' is an emergent,
    session-level property that only a live/integration run can assert
    honestly — a fake just echoes scripted tokens. See Delivery Findings:
    AC2 absolute threshold deferred to a 61-6-style integration validation.
    """
    cost_1h = compute_cost_usd(
        input_tokens=0,
        output_tokens=0,
        cached_input_read_tokens=0,
        cached_input_write_1h_tokens=VOLATILE_TAIL_TOKENS,
        model=SONNET,
    )
    cost_5m = compute_cost_usd(
        input_tokens=0,
        output_tokens=0,
        cached_input_read_tokens=0,
        cached_input_write_5m_tokens=VOLATILE_TAIL_TOKENS,
        model=SONNET,
    )
    cost_uncached = compute_cost_usd(
        input_tokens=VOLATILE_TAIL_TOKENS,
        output_tokens=0,
        cached_input_read_tokens=0,
        model=SONNET,
    )
    assert cost_5m < cost_1h, (
        f"5m write ({cost_5m}) must be cheaper than 1h write ({cost_1h}) for "
        "the same volatile tail — the savings mechanism of this story."
    )
    assert cost_uncached < cost_1h, (
        f"Uncached input ({cost_uncached}) must be cheaper than a 1h write "
        f"({cost_1h}) of the same once-read tail — option (a)'s premise."
    )
    # The third leg of the pricing triangle the design rests on:
    # uncached ($3/M) < 5m-write ($3.75/M) < 1h-write ($6/M). If this inverts,
    # option (a) "uncached" would be strictly cheaper than the chosen 5m tier
    # and the story's read-count justification for 5m collapses.
    assert cost_uncached < cost_5m, (
        f"Uncached input ({cost_uncached}) must be cheaper than a 5m write "
        f"({cost_5m}). If this fires, 5m is dearer than plain input and the "
        "choice of 5m over option (a) needs revisiting."
    )


def test_bounded_tail_flat_but_growing_tail_trips_the_flat_cost_guard() -> None:
    """AC3 (flat-cost model guard — two arms prove it is NOT vacuous).

    With the fix in place the per-turn cost profile is: stable prefix READ
    (cheap, constant) + bounded volatile-tail WRITE at the 5m tier. The
    epic-61 invariant is that per-turn cost stays within 20% of the warmup
    baseline across a long session — no linear creep from a re-written
    growing cache.

    This test models cost as a function of the per-turn tail-write size and
    asserts BOTH directions, so the guard can actually fail:
      - BOUNDED arm: a turn-independent ~1k tail write stays within 20% of
        baseline across turns 5..50 (the post-fix steady state).
      - GROWING arm (control): a tail that creeps linearly (~300 tok/turn — a
        re-written growing cache leaking back in) MUST exceed the 20% bound by
        turn 50. Without this control the bounded assertion would be vacuous
        (it would pass against any always-flat constant).

    NOTE: this guards the cost MODEL's response to growth, not a live session.
    AC3's absolute 50-turn validation is the 61-6-style live run (deferred —
    see Delivery Findings).
    """
    def per_turn_cost(tail_write_tokens: int) -> float:
        # ~25k stable prefix READ + small output are constant; only the 5m
        # volatile-tail write varies (the thing the flat-cost invariant bounds).
        return compute_cost_usd(
            input_tokens=50,
            output_tokens=500,
            cached_input_read_tokens=25_000,
            cached_input_write_5m_tokens=tail_write_tokens,
            model=SONNET,
        )

    bounded_tail = 1_024  # post-fix steady-state tail, same every turn
    baseline = per_turn_cost(bounded_tail)

    # BOUNDED arm — a turn-independent tail stays at baseline, so any later
    # turn is trivially within the 20% bound. A single assertion suffices;
    # the GROWING arm below is what makes this guard able to fail (a 46x loop
    # over the same constant input would be dead iteration — Rule #6).
    assert per_turn_cost(bounded_tail) <= baseline * 1.20, (
        f"A bounded (turn-independent) tail must stay within 20% of baseline "
        f"({baseline}); got {per_turn_cost(bounded_tail)}."
    )

    # GROWING arm (control) — a linearly-creeping tail MUST trip the guard by
    # turn 50, proving the bounded assertion above is not vacuously green.
    growing_tail_at_turn_50 = bounded_tail + 300 * (50 - 5)
    assert per_turn_cost(growing_tail_at_turn_50) > baseline * 1.20, (
        "control failed: a tail growing ~300 tok/turn did NOT exceed the 20% "
        "flat-cost bound by turn 50 — if this fires, the flat-cost assertion "
        "is vacuous (cannot detect a growing re-written cache). "
        f"grown={per_turn_cost(growing_tail_at_turn_50)}, "
        f"bound={baseline * 1.20}."
    )


# ===========================================================================
# Phase B — rule-enforcement (lang-review python.md)
# ===========================================================================


def test_volatile_tier_marker_skip_on_non_dict_block_logs_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule-enforcement (python.md #1 silent-exceptions + No Silent Fallbacks).
    The fix touches the exact marker-attach path that today fails LOUDLY when
    the newest message's last block is a non-dict (a broken upstream
    invariant). The volatile-tier change MUST preserve that loud
    ``logger.warning`` — never silently skip the marker, which would
    re-introduce an unmarked/auto-5m-then-displaced write.
    """
    client = AnthropicSdkClient(sdk=_Sdk(responses=[]), cache_ttl="1h")
    # A non-dict last block (the broken-invariant case the production code
    # guards against): list content whose last element is not a dict.
    running_messages: list[dict[str, Any]] = [
        {"role": "user", "content": ["a bare non-dict block"]}
    ]

    import logging

    with caplog.at_level(logging.WARNING):
        client._build_messages_payload(running_messages, is_continuation=False)  # noqa: SLF001

    assert any(
        record.levelno >= logging.WARNING and "cache_control marker skipped" in record.getMessage()
        for record in caplog.records
    ), (
        "A non-dict last content block MUST trigger a loud logger.warning "
        "('cache_control marker skipped') — No Silent Fallbacks. The "
        f"volatile-tier fix must keep this. Records: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )
