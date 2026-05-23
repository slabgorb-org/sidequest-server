"""Story 61-4 — Output-token-floor + input-bloat fingerprint alarm.

These tests are the RED-phase gate for the runtime half of 61-4 (ACs 2,
3, 6). The orchestrator-side preflight guard lives in a sibling file
under ``scripts/tests/`` (ACs 4, 5, 7).

The alarm wedges into ``AnthropicSdkClient.complete_with_tools`` next
to the existing ``narrator.sdk.usage`` log line. After every successful
SDK call, the client:

1. Maintains two parallel per-instance rolling baselines of length
   K=10 — one for ``cost_usd`` and one for ``input_tokens``.
2. Compares the just-observed call against the baselines (or the
   warmup floors before K observations have accumulated).
3. Fires a single ``cost_runaway_suspected`` watcher event
   (``severity="warn"``) + a single ``logger.error`` line if either
   trigger fires:

   - **Cost trigger:** ``cost_usd > 5 × baseline_cost``
     (warmup floor: 5 × $0.03 = $0.15)
   - **I/O fingerprint trigger:** ``input_tokens > 2 × baseline_input
     AND output_tokens < 50`` (warmup floor: 2 × 12_000 = 24_000)

4. Adds the call to both baselines after the check (so the alarm
   compares against PRIOR calls, never against itself).

Why this matters: the 2026-05-23 $313 runaway was a 60K-in/12-out
shape hammered through 8 tool-loop iterations per turn for hours. No
existing observability noticed because per-call cost ($0.18) is in
the noise vs. the daily aggregate; only the *shape* is anomalous. The
I/O fingerprint trigger catches this without waiting for cost
aggregation. (See ``sprint/context/context-story-61-4.md`` for the
full decision rationale A-F.)

The locked design is documented at:
``sprint/context/context-story-61-4.md`` — decisions A (two parallel
baselines), B (warmup floors $0.03 + 12_000), C (single event with
``trigger`` field), F (test placement).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import pytest

from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
)
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub

# --- SDK-shape fakes (mirror tests/agents/test_60_4_continuation_cache_breakpoint.py) --


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


def _resp(
    *,
    input_tokens: int,
    output_tokens: int,
    text: str = "ok",
    stop_reason: str = "end_turn",
    cache_read: int = 0,
    cache_write: int = 0,
    model: str = "claude-sonnet-4-6",
) -> _Resp:
    """One scripted SDK response with explicit token shape."""
    return _Resp(
        content=[_TextBlock(type="text", text=text)],
        stop_reason=stop_reason,
        usage=_Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
        model=model,
    )


def _healthy() -> _Resp:
    """Reference healthy turn shape: ~12K in / ~500 out (60-4 baseline)."""
    return _resp(input_tokens=12_000, output_tokens=500)


def _runaway_fingerprint() -> _Resp:
    """The 2026-05-23 incident's exact shape: 60K-in / 12-out."""
    return _resp(input_tokens=60_000, output_tokens=12)


def _system_blocks() -> list[CacheableBlock]:
    return [CacheableBlock(text="rules", cache=True)]


def _user_msg() -> list[Message]:
    return [Message(role="user", content="go")]


def _tools_empty() -> list[ToolDefinition]:
    # No tool_use in any scripted response → tool_dispatch is never called.
    return []


class _FakeSocket:
    """Minimal ``_Sendable`` for watcher_hub subscription. Collects every
    published event so tests can assert delivery to the GM-panel transport
    (not just ``logger.error``). Same pattern as 61-3 tests."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the watcher hub to the test event loop and clear subscribers.

    Matches the pattern in ``tests/agents/test_61_3_hard_cap_oversized_canary.py``.
    """
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


def _build_client(sdk: _Sdk) -> AnthropicSdkClient:
    """Construct a client wired to the fake SDK; cache_ttl=1h matches
    production default (ADR-101 + 60-4)."""
    return AnthropicSdkClient(sdk=sdk, cache_ttl="1h")


# ---------------------------------------------------------------------------
# 1. AC6 — Synthesize 60K-in/12-out call, assert alarm fires exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_io_fingerprint_60k_in_12_out_fires_alarm_once(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC6 (regression test, runtime path). The exact 2026-05-23 incident
    shape — 60K input tokens, 12 output tokens, no cache rebate — MUST
    produce exactly one ``cost_runaway_suspected`` watcher event AND
    exactly one ERROR-level log record carrying
    ``narrator.cost_runaway_suspected``.

    This is the regression that, had it existed before 2026-05-23, would
    have caught the runaway on call #1 instead of $313 later.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_runaway_fingerprint()])
    client = _build_client(sdk)

    with caplog.at_level(logging.DEBUG, logger="sidequest.agents.anthropic_sdk_client"):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
        )
    await asyncio.sleep(0.05)

    runaway_events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(runaway_events) == 1, (
        "Exactly one cost_runaway_suspected event must reach watcher "
        f"subscribers per offending call; got {len(runaway_events)} "
        f"(all events: {[e.get('event_type') for e in sock.events]})."
    )

    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "narrator.cost_runaway_suspected" in r.getMessage()
    ]
    assert len(error_records) == 1, (
        "Exactly one ERROR-level log record carrying "
        "'narrator.cost_runaway_suspected' must accompany the watcher emit "
        f"(GM-panel + log-tail parity). Got {len(error_records)} "
        f"records; all records: {[r.getMessage() for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# 2. AC2 — Severity is warn (NOT error, NOT info) and trigger field is io_fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_io_fingerprint_event_severity_is_warn_with_trigger_field(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC2 + decision C. ``severity="warn"`` (NOT 61-3's "error" — the
    fingerprint detector is a suspicion, not a hard cap). Single event
    type with a ``trigger`` discriminator field so GM-panel filters can
    drill into io_fingerprint vs cost_multiple causes.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_runaway_fingerprint()])
    client = _build_client(sdk)
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
    )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(events) == 1, f"need one event; got {len(events)}"
    event = events[0]

    assert event.get("severity") == "warn", (
        "AC2 explicit: cost_runaway_suspected MUST use severity='warn' "
        "(distinct from 61-3's 'error'). The fingerprint detector is a "
        f"suspicion, not a hard cap. Got severity={event.get('severity')!r}."
    )
    fields = event.get("fields", {})
    assert fields.get("trigger") == "io_fingerprint", (
        "Per decision C: single event type with a 'trigger' discriminator "
        "field; 60K-in/12-out shape MUST report trigger='io_fingerprint'. "
        f"Got trigger={fields.get('trigger')!r}; fields={fields}"
    )
    # Operator-actionable payload: must surface the offending shape AND
    # the baseline it was compared against, so the GM panel renders both.
    for key in ("input_tokens", "output_tokens", "cost_usd", "baseline_input_tokens", "warmup"):
        assert key in fields, (
            f"GM panel needs '{key}' on cost_runaway_suspected fields; got fields={list(fields)}."
        )
    assert fields["input_tokens"] == 60_000, fields
    assert fields["output_tokens"] == 12, fields
    assert fields["warmup"] is True, (
        "Call #1 must report warmup=True so the GM panel can show "
        f"'baseline is floor, not observed history'. Got warmup={fields.get('warmup')!r}"
    )


# ---------------------------------------------------------------------------
# 3. AC3 — Rolling baseline length K=10; 11th call excludes 1st
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rolling_baseline_window_is_k10_and_excludes_oldest(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC3: baseline is per-session, computed over the **last K=10
    calls**. After 10 healthy calls, the 11th call's comparison baseline
    must exclude the 1st call (fixed-size deque behavior).

    Concrete assertion: feed 10 healthy calls (12K in, 500 out) → baseline
    input ≈ 12_000. Then feed an 11th healthy call → still ≈ 12_000.
    Then feed a 12th call at 12K in / 12 out — output<50 BUT input is at
    baseline (not 2x), so the I/O trigger MUST NOT fire (proves the
    baseline is being used, not the warmup floor of 12_000-treated-as-half).
    Then a 13th call at 25_000 in / 12 out → MUST fire (input > 2 × ~12K).

    If the implementation kept all-history baseline (no K=10 cap), the
    baseline would be the same so the test would pass for the wrong
    reason — that's covered by test 4 below which proves window eviction.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # 10 healthy calls warm the baseline up to K=10 observations.
    sdk = _Sdk(
        responses=[_healthy() for _ in range(10)] + [_resp(input_tokens=25_000, output_tokens=12)]
    )
    client = _build_client(sdk)
    for _ in range(11):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
        )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(events) == 1, (
        "After 10 healthy warmup calls, the 11th call at 25K in / 12 out "
        "MUST trip the I/O fingerprint (25_000 > 2 × baseline≈12_000). "
        f"Got {len(events)} cost_runaway_suspected events. "
        "If 0: baseline not being used. If >1: alarm fired during warmup too."
    )
    fields = events[0].get("fields", {})
    assert fields.get("warmup") is False, (
        "After K=10 observations, baseline is observed (not floor). "
        f"Got warmup={fields.get('warmup')!r}"
    )
    # Baseline input should reflect the 10 healthy calls averaging ~12_000.
    assert 11_000 <= fields.get("baseline_input_tokens", 0) <= 13_000, (
        "baseline_input_tokens MUST be the rolling-K=10 mean of observed "
        "input_tokens (~12_000 after 10 healthy calls of 12_000 each). "
        f"Got baseline_input_tokens={fields.get('baseline_input_tokens')!r}"
    )


@pytest.mark.asyncio
async def test_rolling_window_evicts_oldest_after_k_plus_one_calls(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC3 sharper: prove the K=10 window evicts the oldest call.

    Sequence: 1 anomalous call at 100K input (NOT 60K-in/12-out — output
    is healthy so I/O trigger DOESN'T fire, but it pollutes the input
    baseline), followed by 10 healthy calls at 12K input. After call 11,
    if the window is K=10, the 100K outlier has been evicted and baseline
    input ≈ 12_000. If the window is unbounded (or longer than 10), the
    baseline includes the 100K outlier and is ≈ 20_000.

    Then call 12 at 25K input / 12 output:
      - K=10 window (correct): baseline ≈ 12K → 25K > 24K trips alarm.
      - Unbounded window (bug): baseline ≈ 20K → 25K < 40K, NO alarm.

    Different test, separate trigger condition, same K=10 invariant — a
    deliberate adversarial probe against the off-by-one of "I keep
    everything" / "K=11" / etc.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # Use output_tokens=500 on the 100K call so it pollutes baseline
    # WITHOUT itself tripping I/O fingerprint (output >= 50).
    # Use output 500 on healthy calls (won't trip either).
    # 12th call is the probe at 25K in / 12 out.
    responses = (
        [_resp(input_tokens=100_000, output_tokens=500)]  # pollute
        + [_healthy() for _ in range(10)]  # evict the outlier
        + [_resp(input_tokens=25_000, output_tokens=12)]  # probe
    )
    sdk = _Sdk(responses=responses)
    client = _build_client(sdk)
    for _ in range(12):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
        )
    await asyncio.sleep(0.05)

    # Call #1 (100K in / 500 out): warmup, input > 24_000 floor, BUT
    # output >= 50 → no I/O trigger. Cost: 100K × $3/M = $0.30 > $0.15
    # floor → cost_multiple DOES trigger. So we expect 1 warmup-fire on
    # call #1 (cost_multiple), then no fires on calls #2-11, then a
    # second fire on call #12 only if K=10 window correctly evicted #1.
    runaway_events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    triggers = [e.get("fields", {}).get("trigger") for e in runaway_events]
    assert "io_fingerprint" in triggers, (
        "Call #12 (25K-in/12-out) MUST trip io_fingerprint trigger if K=10 "
        "window correctly evicted the call-#1 100K outlier. Triggers seen: "
        f"{triggers}. If io_fingerprint missing, baseline window is wider "
        "than K=10 (eviction didn't happen)."
    )


# ---------------------------------------------------------------------------
# 4. AC3 — Warmup floor: first call uses $0.03 / 12_000 floor, can trip alarm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_call_uses_floor_and_can_trip_cost_trigger(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """AC3 + decision B: before K=10 calls accumulate, comparison uses
    fixed floors ($0.03/turn for cost, 12_000 input tokens for I/O).

    Cost trigger fires when cost_usd > 5 × $0.03 = $0.15. A single call
    with 60K input + 100 output (no cache) costs ≈ 60K × $3/M + 100 ×
    $15/M = $0.180 + $0.0015 = $0.1815 > $0.15 floor → fire.

    Output is 100 (>= 50), so the I/O fingerprint trigger does NOT fire
    — this isolates the cost_multiple trigger against the warmup floor.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_resp(input_tokens=60_000, output_tokens=100)])
    client = _build_client(sdk)
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
    )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(events) == 1, (
        "First call at 60K-in/100-out costs ~$0.18 > $0.15 warmup floor; "
        f"MUST fire cost_runaway_suspected. Got {len(events)} events."
    )
    fields = events[0].get("fields", {})
    assert fields.get("trigger") == "cost_multiple", (
        "Output=100 (>=50) precludes io_fingerprint; trigger MUST be "
        f"'cost_multiple'. Got trigger={fields.get('trigger')!r}"
    )
    assert fields.get("warmup") is True, (
        f"First call MUST report warmup=True. Got warmup={fields.get('warmup')!r}"
    )
    # Floor was used for comparison — surface it for operator clarity.
    assert abs(fields.get("baseline_cost_usd", 0) - 0.03) < 1e-9, (
        "Warmup baseline_cost_usd MUST be the locked $0.03 floor (decision B). "
        f"Got baseline_cost_usd={fields.get('baseline_cost_usd')!r}"
    )


@pytest.mark.asyncio
async def test_healthy_first_call_under_floor_does_not_trip(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """No false positives during warmup. A healthy 12K-in/500-out call
    is well under both floors: cost ≈ $0.044 (< $0.15), input 12_000
    (not > 24_000), output 500 (>= 50). MUST NOT fire."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_healthy()])
    client = _build_client(sdk)
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
    )
    await asyncio.sleep(0.05)

    runaway_events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert runaway_events == [], (
        "A healthy 12K-in/500-out call MUST NOT trip any alarm during "
        f"warmup (regression guard against floor-set-too-low). Got "
        f"{len(runaway_events)} events: {runaway_events}"
    )


# ---------------------------------------------------------------------------
# 5. AC2 — Exactly-once-per-offending-call (no spam during sustained runaway)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sustained_runaway_emits_one_event_per_call_not_per_iteration(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sustained-runaway observability-spam guard, matching 61-3's pattern.

    Three consecutive runaway-shape calls must produce exactly three
    cost_runaway_suspected events + three ERROR log lines — not 3xN from
    an internal loop or per-tool-loop-iteration emit. If a single
    complete_with_tools call internally fired the alarm multiple times,
    the GM panel would drown during a real cost runaway.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_runaway_fingerprint() for _ in range(3)])
    client = _build_client(sdk)

    with caplog.at_level(logging.DEBUG, logger="sidequest.agents.anthropic_sdk_client"):
        for _ in range(3):
            await client.complete_with_tools(
                system_blocks=_system_blocks(),
                messages=_user_msg(),
                tools=_tools_empty(),
                model="claude-sonnet-4-6",
            )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(events) == 3, (
        "Each offending call must emit exactly one event (no internal "
        f"loop, no recursive emit). Got {len(events)} events across 3 calls."
    )
    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "narrator.cost_runaway_suspected" in r.getMessage()
    ]
    assert len(error_records) == 3, (
        f"Log parity: 3 calls → 3 ERROR records, not 3xN. Got {len(error_records)} ERROR records."
    )


# ---------------------------------------------------------------------------
# 6. AC2 / decision C — io_fingerprint AND cost_multiple in same call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_triggers_active_simultaneously_emit_single_event_with_io_priority(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """The 60K-in/12-out runaway shape trips BOTH triggers (cost > $0.15
    floor AND I/O fingerprint matches). The alarm MUST still emit exactly
    one event per call — not two (one per trigger) which would double-spam
    the GM panel and double-count ERROR records.

    The ``trigger`` field reports the I/O fingerprint as primary
    (decision C — the I/O signature is the more diagnostic of the two,
    matching the 2026-05-23 incident's fingerprint). The cost-multiple
    condition is still surfaced through the cost_usd / baseline_cost_usd
    field pair so the operator sees the full picture in one event.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_runaway_fingerprint()])
    client = _build_client(sdk)
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
    )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(events) == 1, (
        "Both-triggers-active MUST collapse to a single event per call "
        f"(no double-spam). Got {len(events)} events. Triggers: "
        f"{[e.get('fields', {}).get('trigger') for e in events]}"
    )
    fields = events[0].get("fields", {})
    assert fields.get("trigger") == "io_fingerprint", (
        "Per decision C: when both fire simultaneously, the I/O "
        "fingerprint is the primary discriminator (it matches the "
        "2026-05-23 incident signature exactly). Got "
        f"trigger={fields.get('trigger')!r}"
    )


# ---------------------------------------------------------------------------
# 7. Architect spec-check A — reset_baselines() clears rolling state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_baselines_clears_rolling_state(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """``RoomRegistry`` never evicts a slug (session_room.py:774-786), so
    ``AnthropicSdkClient`` lives for the process lifetime per slug. Without
    a reset on slug recycle, the rolling baseline can self-train onto a
    sustained runaway and silence the alarm. ``reset_baselines()`` MUST
    clear both deques so the next session starts cold (warmup floor active
    again).

    Sequence: warm baseline to K=10 with healthy calls. Confirm next call
    sees the OBSERVED baseline (warmup=False). Call ``reset_baselines()``.
    Confirm the very next call sees warmup floors (warmup=True) again.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # 10 healthy warmup calls + 1 post-warmup probe that trips io_fingerprint
    # so we capture warmup=False; then a post-reset probe at the same shape
    # that MUST report warmup=True.
    responses = (
        [_healthy() for _ in range(10)]
        + [_resp(input_tokens=25_000, output_tokens=12)]  # probe pre-reset
        + [_resp(input_tokens=25_000, output_tokens=12)]  # probe post-reset
    )
    sdk = _Sdk(responses=responses)
    client = _build_client(sdk)

    # Warm baseline to K=10 + first probe (post-warmup).
    for _ in range(11):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
        )
    await asyncio.sleep(0.05)

    # Sanity: deques are full at K=10 (the eleventh probe already evicted
    # the oldest entry; deque maxlen=10).
    assert len(client._cost_baseline) == 10, (  # noqa: SLF001
        f"K=10 deque should hold 10 entries after 11 calls; got {len(client._cost_baseline)}"  # noqa: SLF001
    )

    # First probe (call #11) should have reported warmup=False.
    pre_reset_events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(pre_reset_events) >= 1, (
        "Pre-reset probe at 25K-in/12-out MUST trip alarm with observed "
        f"baseline. Got {len(pre_reset_events)} events."
    )
    assert pre_reset_events[-1]["fields"]["warmup"] is False, (
        "Pre-reset probe MUST report warmup=False (baseline is observed)."
    )

    # The reset.
    client.reset_baselines()
    assert len(client._cost_baseline) == 0, (  # noqa: SLF001
        f"reset_baselines() MUST clear cost deque; got {len(client._cost_baseline)}"  # noqa: SLF001
    )
    assert len(client._input_tokens_baseline) == 0, (  # noqa: SLF001
        f"reset_baselines() MUST clear input_tokens deque; got {len(client._input_tokens_baseline)}"  # noqa: SLF001
    )

    # Post-reset probe MUST see warmup floors again (warmup=True).
    sock.events.clear()
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
    )
    await asyncio.sleep(0.05)

    post_reset_events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(post_reset_events) == 1, (
        f"Post-reset probe MUST trip alarm (25K > 2 × 12_000 warmup floor "
        f"AND output<50). Got {len(post_reset_events)} events."
    )
    assert post_reset_events[0]["fields"]["warmup"] is True, (
        "After reset_baselines(), the next call MUST report warmup=True "
        "(rolling state cleared, warmup floor active again). Got "
        f"warmup={post_reset_events[0]['fields'].get('warmup')!r}"
    )


# ---------------------------------------------------------------------------
# 8. Architect spec-check A — absolute cost floor fires when baseline is high
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absolute_cost_floor_fires_when_baseline_is_high(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """The rolling baseline can self-train onto a sustained runaway,
    silencing the cost_multiple trigger. The ``$0.30`` absolute floor is
    the safety net: it MUST fire even when the offending call is sub-5x
    the (high) rolling baseline.

    Sequence: train the cost baseline up to ~$0.18/call across K=10
    consecutive runaway-shaped turns (60K-in / 500-out costs ~$0.1875 — we
    use 500 output to keep the I/O fingerprint silent so the only trigger
    that could speak is cost-related). After K=10 the baseline is ~$0.187.
    Then fire a $0.31 call (input ~100K, output 500): 0.31 / 0.187 ≈ 1.66
    — sub-5x baseline (cost_multiple silent) but > $0.30 absolute floor.
    MUST emit one event with ``trigger="cost_absolute"``.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # 10 sustained runaways at 60K in / 500 out (cost ≈ $0.1875 each) +
    # one $0.31-shaped probe (100K in / 500 out ≈ $0.3075).
    sustained = _resp(input_tokens=60_000, output_tokens=500)
    probe = _resp(input_tokens=100_000, output_tokens=500)
    sdk = _Sdk(responses=[sustained for _ in range(10)] + [probe])
    client = _build_client(sdk)

    for _ in range(11):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
        )
    await asyncio.sleep(0.05)

    # The 10 warmup calls each trip cost_multiple (>$0.15 warmup floor)
    # AND every call after warmup that exceeds $0.30 trips cost_absolute.
    # Filter to events from the 11th call (post-warmup probe).
    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    post_warmup = [e for e in events if e["fields"]["warmup"] is False]
    assert len(post_warmup) == 1, (
        "Exactly one post-warmup event expected from the $0.31 probe; got "
        f"{len(post_warmup)} (all post-warmup events: {post_warmup})."
    )
    fields = post_warmup[0]["fields"]
    # The probe at $0.3075 with a baseline of ~$0.1875 is only ~1.64x
    # baseline — well below the 5x cost_multiple threshold. The ONLY way
    # this event can fire is the absolute floor.
    assert fields["trigger"] == "cost_absolute", (
        "Post-warmup $0.31 call with high (~$0.19) trained baseline is "
        "sub-5x baseline (cost_multiple should be silent) AND output>=50 "
        "(io_fingerprint silent). The only remaining trigger is the "
        f"$0.30 absolute floor. Got trigger={fields['trigger']!r}, "
        f"cost_usd={fields['cost_usd']!r}, "
        f"baseline_cost_usd={fields['baseline_cost_usd']!r}"
    )
    assert fields["cost_usd"] > _ABSOLUTE_COST_USD_FLOOR_PROBE, (
        f"Probe must exceed the absolute floor. Got cost_usd={fields['cost_usd']!r}"
    )


# ---------------------------------------------------------------------------
# 9. Architect spec-check A — io_fingerprint priority preserved over absolute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absolute_floor_does_not_re_fire_io_fingerprint_priority(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """When io_fingerprint AND the new absolute cost floor would BOTH
    trigger on the same call, the event MUST collapse to a single event
    with ``trigger="io_fingerprint"`` (preserves the original decision C
    priority — io_fingerprint is the most diagnostic shape and matches
    the 2026-05-23 incident exactly).

    Sequence: a fresh client, then one 60K-in / 12-out call. That single
    call trips:
    - io_fingerprint (60K > 2 × 12K floor AND output<50)
    - cost_multiple (~$0.18 > 5 × $0.03 warmup floor)
    - cost_absolute would NOT trip on the warmup-shape (0.18 < 0.30), so
      this test specifically sizes the call so it trips ALL three: 200K
      input / 12 output. Cost ≈ $0.60 (>$0.30 absolute) AND output<50
      with input>>floor (io_fingerprint) AND >>5x floor (cost_multiple).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # 200K input / 12 output trips all three triggers simultaneously.
    sdk = _Sdk(responses=[_resp(input_tokens=200_000, output_tokens=12)])
    client = _build_client(sdk)
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
    )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(events) == 1, (
        "Triple-trigger call (io_fingerprint + cost_multiple + "
        "cost_absolute) MUST collapse to one event (no double-spam). "
        f"Got {len(events)}."
    )
    assert events[0]["fields"]["trigger"] == "io_fingerprint", (
        "Priority order is io_fingerprint > cost_multiple > "
        "cost_absolute. When all three fire, io_fingerprint wins "
        f"(decision C, preserved). Got trigger={events[0]['fields']['trigger']!r}"
    )


# Probe constant — kept local to this module so the test asserts against
# the documented floor rather than re-importing the implementation detail.
_ABSOLUTE_COST_USD_FLOOR_PROBE: float = 0.30


# ---------------------------------------------------------------------------
# 10. TEA verify (adversarial A-attack) — silence-prevention end-to-end
# ---------------------------------------------------------------------------
#
# This is a *stronger* shape than test 8 above. Test 8 trains the baseline
# at $0.18/call (warmup-floor-tripping shape) then fires $0.31 and asserts
# `trigger=="cost_absolute"`. The user's A-attack spec asks the inverse:
# train at the EXPLICITLY sub-$0.15-warmup-floor shape ($0.12/call), so
# cost_multiple is silent both during warmup AND post-warmup, then fire a
# $0.31 probe — and assert BOTH (a) cost_absolute fires AND (b) no
# cost_multiple event has fired across the ENTIRE 11-call sequence. This
# is the real-world silence-prevention claim: "sustained sub-warmup-floor
# traffic followed by a $0.31 spike still alarms via the safety net."
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tea_adversarial_a_attack_baseline_self_training(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Adversarial A-attack probe (TEA verify, 2026-05-23).

    Per Sonnet rates ($3/MTok input, $15/MTok output): a 40K-in / 500-out
    call costs $0.120 + $0.0075 ≈ $0.1275 — *under* the $0.15 warmup
    floor, so cost_multiple stays silent during warmup. After 10 such
    calls the rolling baseline is ~$0.1275. Then probe at 102K-in /
    500-out ≈ $0.3135: 0.3135 / 0.1275 ≈ 2.46x baseline (sub-5x →
    cost_multiple still silent), output=500 (io_fingerprint silent),
    but $0.3135 > $0.30 absolute floor → cost_absolute MUST fire.

    Negative half of the contract: ZERO cost_multiple events MAY appear
    across the whole sequence. This is the part the existing
    test_absolute_cost_floor_fires_when_baseline_is_high does NOT assert
    (it only checks the post-warmup probe is cost_absolute, not that the
    cost_multiple lane stayed dark).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # 10 "sub-warmup-floor" calls at 40K/500 (~$0.1275 each) +
    # 1 probe at 102K/500 (~$0.3135).
    sustained = _resp(input_tokens=40_000, output_tokens=500)
    probe = _resp(input_tokens=102_000, output_tokens=500)
    sdk = _Sdk(responses=[sustained for _ in range(10)] + [probe])
    client = _build_client(sdk)

    for _ in range(11):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
        )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]

    # Positive: exactly one cost_absolute event, on the probe.
    cost_abs = [e for e in events if e["fields"]["trigger"] == "cost_absolute"]
    assert len(cost_abs) == 1, (
        "A-attack: $0.31 probe after 10x $0.12 turns MUST fire "
        f"cost_absolute exactly once; got {len(cost_abs)} cost_absolute "
        f"events. All events: "
        f"{[(e['fields'].get('trigger'), e['fields'].get('cost_usd')) for e in events]}"
    )
    fields = cost_abs[0]["fields"]
    assert fields["warmup"] is False, (
        "Probe fires AFTER K=10 sustained calls — baseline is OBSERVED "
        f"(not floor). Got warmup={fields['warmup']!r}"
    )
    assert fields["cost_usd"] > _ABSOLUTE_COST_USD_FLOOR_PROBE, (
        f"Probe cost_usd must exceed $0.30 absolute floor; got cost_usd={fields['cost_usd']!r}"
    )
    # Baseline must reflect the trained $0.1275, not the warmup floor.
    assert 0.10 <= fields["baseline_cost_usd"] <= 0.15, (
        "Baseline must reflect 10 trained calls (~$0.1275), not the "
        f"$0.03 warmup floor. Got baseline_cost_usd={fields['baseline_cost_usd']!r}"
    )

    # Negative: ZERO cost_multiple events anywhere in the 11-call sequence.
    # If this fires, the sub-warmup-floor calls aren't actually sub-floor
    # (Sonnet rates drifted) OR the probe somehow tripped cost_multiple
    # at 2.46x baseline (5x threshold misconfigured).
    cost_mult = [e for e in events if e["fields"]["trigger"] == "cost_multiple"]
    assert len(cost_mult) == 0, (
        "A-attack negative half: cost_multiple MUST stay silent. "
        "Warmup calls at $0.1275 are under the $0.15 warmup floor; "
        "probe at $0.31 is 2.46x baseline (sub-5x). Got "
        f"{len(cost_mult)} cost_multiple events: "
        f"{[(e['fields'].get('cost_usd'), e['fields'].get('baseline_cost_usd')) for e in cost_mult]}"
    )
    # And no io_fingerprint either — output=500 throughout.
    io_fp = [e for e in events if e["fields"]["trigger"] == "io_fingerprint"]
    assert len(io_fp) == 0, (
        "A-attack: io_fingerprint MUST stay silent (output=500 >> 50). "
        f"Got {len(io_fp)} io_fingerprint events."
    )
