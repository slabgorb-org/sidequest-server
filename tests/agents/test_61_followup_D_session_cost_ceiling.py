"""Story 61-followup-D — Session-cumulative HARD KILL at $10.00 (mitigation C).

The structural backstop the project did not have when $313 burned in
48h on 2026-05-23. Three sub-components, all in scope:

1. Per-``session_id`` cumulative cost tracker on
   ``AnthropicSdkClient`` (a ``dict[str, float]``).
2. ``AnthropicSdkCostCeilingExceeded`` typed exception raised when
   cumulative crosses ``_SESSION_COST_CEILING_USD`` (default $10.00,
   overridable via ``SIDEQUEST_SESSION_COST_CEILING_USD``).
3. Two new watcher events:
   - ``session.cost_ceiling_exceeded`` (severity ``"error"``) at
     threshold-cross, once per session.
   - ``session.cost_running_total`` (severity ``"info"``) every turn
     for the GM-panel live counter.

Refusal is terminal — no fallback, no silent degraded path. After the
ceiling crosses, every subsequent call for the same ``session_id``
re-raises.

These tests are the RED gate. See
``sprint/context/context-story-61-followup-D.md`` §C + §D for the
locked decisions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from sidequest.agents.anthropic_sdk_client import (
    _SESSION_COST_CEILING_USD,
    AnthropicSdkClient,
    AnthropicSdkConfigError,
    AnthropicSdkCostCeilingExceeded,
)
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub
from tests.agents.test_61_4_cost_runaway_alarm import (  # type: ignore[attr-defined]
    _FakeSocket,
    _resp,
    _Sdk,
    _system_blocks,
    _tools_empty,
    _user_msg,
)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


def _build_client(
    sdk: _Sdk,
    *,
    ceiling: float | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> AnthropicSdkClient:
    """Construct a client with an optional ceiling override. The ceiling
    override is exercised by setting the env var (mirrors the manual
    playtest closure step where the operator artificially lowers the
    ceiling to $0.50)."""
    if ceiling is not None:
        assert monkeypatch is not None, "monkeypatch required when overriding ceiling"
        monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", str(ceiling))
    return AnthropicSdkClient(sdk=sdk, cache_ttl="1h")


# Big-input small-output shape costs almost exactly $0.30/call:
# 100_000 * $3 / 1_000_000 + 200 * $15 / 1_000_000 = $0.300 + $0.003 =
# $0.303. Two such calls hit $0.606; ~33 cross $10.00. For tighter
# tests we use a $0.50 ceiling so the threshold lands inside 2 calls.
def _heavy_call() -> Any:
    return _resp(input_tokens=100_000, output_tokens=200)


# Tiny call: 1_000-in / 50-out costs ~$0.0038. Useful for budget-safe
# rejoin-inheritance tests.
def _tiny_call() -> Any:
    return _resp(input_tokens=1_000, output_tokens=50)


# ---------------------------------------------------------------------------
# 1. Default ceiling is $10.00 and env var overrides it
# ---------------------------------------------------------------------------


def test_default_ceiling_is_ten_dollars() -> None:
    """Story body §C names $10.00 as the structural backstop. Drift
    detector: a future tweak to a different number must come with a
    story-body update and a test review.
    """
    assert _SESSION_COST_CEILING_USD == 10.0, (
        f"Story §C locks the default ceiling at $10.00. Got "
        f"{_SESSION_COST_CEILING_USD!r}."
    )


def test_env_var_override_parses_positive_float(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SIDEQUEST_SESSION_COST_CEILING_USD`` mirrors the cache TTL env
    var pattern (anthropic_sdk_client.py:113). The override path is
    load-bearing for the manual-playtest closure (story §Testing —
    operator lowers ceiling to $0.50, observes graceful termination).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "0.50")
    sdk = _Sdk(responses=[])
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")
    # The client must read the override during construction and expose
    # it for verification; tests should not need to reach into private
    # state. A documented attribute or property is acceptable.
    assert client.session_cost_ceiling_usd == pytest.approx(0.50), (
        "SIDEQUEST_SESSION_COST_CEILING_USD=0.50 MUST resolve to 0.50 "
        "on the client (mirroring the cache_ttl env var pattern). Got "
        f"client.session_cost_ceiling_usd={getattr(client, 'session_cost_ceiling_usd', '<missing>')!r}"
    )


def test_env_var_override_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """No silent fallback (CLAUDE.md). A non-parseable or non-positive
    override MUST raise ``AnthropicSdkConfigError`` at construction
    time — same shape as the cache_ttl validation at line 115.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[])

    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "not-a-number")
    with pytest.raises(AnthropicSdkConfigError):
        AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "-1.0")
    with pytest.raises(AnthropicSdkConfigError):
        AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "0")
    with pytest.raises(AnthropicSdkConfigError):
        AnthropicSdkClient(sdk=sdk, cache_ttl="1h")


# ---------------------------------------------------------------------------
# 2. Crossing the ceiling raises AnthropicSdkCostCeilingExceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crossing_ceiling_raises_typed_exception(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Story §C.2: hard kill via typed exception, not return-value flag.
    The call that pushes cumulative across the ceiling MUST raise
    ``AnthropicSdkCostCeilingExceeded``. The exception carries the
    session_id, the cumulative figure, and the ceiling.

    With ceiling=$0.50 and a $0.303 heavy call, call #1 succeeds
    (cumulative=$0.303 < $0.50). Call #2 either pre-flight refuses (if
    the implementation refuses before billing when cumulative would
    exceed) OR completes and then raises post-billing. EITHER is
    acceptable per story design (§C.2 documents both). We assert the
    raise happens by call #2 at latest.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_heavy_call(), _heavy_call()])
    client = _build_client(sdk, ceiling=0.50, monkeypatch=monkeypatch)

    # Call #1 succeeds.
    result1 = await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="ceiling-cross-test",
    )
    assert result1.cumulative_cost_usd == pytest.approx(0.303, abs=1e-3), (
        f"Call #1 cumulative_cost_usd should be ~$0.303. Got "
        f"{result1.cumulative_cost_usd!r}"
    )

    # Call #2 crosses the ceiling.
    with pytest.raises(AnthropicSdkCostCeilingExceeded) as excinfo:
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="ceiling-cross-test",
        )
    err = excinfo.value
    assert err.session_id == "ceiling-cross-test", (
        f"Exception session_id MUST match the offending session. Got "
        f"{err.session_id!r}"
    )
    assert err.ceiling_usd == pytest.approx(0.50), (
        f"Exception ceiling_usd MUST match configured ceiling. Got "
        f"{err.ceiling_usd!r}"
    )
    assert err.cumulative_cost_usd >= 0.50, (
        "Exception cumulative MUST be at or above the ceiling — that's "
        "the whole reason it was raised. Got "
        f"cumulative_cost_usd={err.cumulative_cost_usd!r}"
    )


# ---------------------------------------------------------------------------
# 3. Refusal is terminal — subsequent calls keep raising
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsequent_calls_after_ceiling_keep_raising(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Story §C body: "Refusal is terminal for the session — no fallback,
    no silent degraded path." After the ceiling has been crossed, every
    subsequent ``complete_with_tools`` for the same ``session_id`` MUST
    re-raise ``AnthropicSdkCostCeilingExceeded`` WITHOUT making an SDK
    call.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_heavy_call(), _heavy_call()])
    client = _build_client(sdk, ceiling=0.50, monkeypatch=monkeypatch)

    # Drive past the ceiling.
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="terminal-test",
    )
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="terminal-test",
        )

    # Snapshot how many SDK calls have been made so far. Subsequent
    # refusals MUST NOT make new SDK calls — that's the "no further
    # tokens billed" guarantee.
    calls_before_refuse = len(sdk.messages.calls)

    # Third call — must also raise without touching the SDK.
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="terminal-test",
        )
    # Fourth call — same.
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="terminal-test",
        )

    assert len(sdk.messages.calls) == calls_before_refuse, (
        "After the ceiling is crossed, subsequent refusals MUST NOT "
        "call sdk.messages.create — the whole point of the hard kill "
        "is to stop billing. Got "
        f"{len(sdk.messages.calls)} SDK calls (expected "
        f"{calls_before_refuse} — no growth)."
    )


# ---------------------------------------------------------------------------
# 4. Cumulative cost is per-session_id — distinct sessions don't pollute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cumulative_cost_is_per_session_id(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Story §C.1: cumulative cost lives on ``session_id``, NOT on the
    AnthropicSdkClient instance. Session A burning $0.40 must NOT push
    session B over the same ceiling. Memory: project_session_id_dropin
    + the 61-followup-A "cross-session pollution on slug-reuse" hazard.

    With ceiling=$0.50:
    - Session A makes one $0.303 heavy call (cumulative_A = $0.303).
    - Session B makes one $0.303 heavy call (cumulative_B = $0.303,
      independent of A).
    - Both still below their ceilings; neither raises.

    Sanity: session A's NEXT heavy call would cross (cumulative_A →
    $0.606 > $0.50). After A's kill fires, session B makes a tiny
    call: B inherits only its own $0.303 cumulative (NOT polluted by
    A's billing) so the tiny call succeeds without crossing.

    Two heavy calls per session would push both over independently —
    the proof is "A crosses, B does NOT cross at the moment A crosses".
    The tiny call after A's kill is the cleanest probe for that.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # 4 responses: A1 heavy, B1 heavy, A2 heavy (crosses A), B-tiny.
    sdk = _Sdk(
        responses=[_heavy_call(), _heavy_call(), _heavy_call(), _tiny_call()]
    )
    client = _build_client(sdk, ceiling=0.50, monkeypatch=monkeypatch)

    # A1 + B1: both safe.
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="session-A",
    )
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="session-B",
    )

    # A2 — session A's cumulative crosses → raise.
    with pytest.raises(AnthropicSdkCostCeilingExceeded) as excinfo:
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="session-A",
        )
    assert excinfo.value.session_id == "session-A", (
        f"Crossing call MUST raise with session_id='session-A'. Got "
        f"{excinfo.value.session_id!r}"
    )

    # B-tiny — session B is at $0.303 from B1; adding a tiny ~$0.004
    # call lands at ~$0.307, still below the $0.50 ceiling. If B had
    # inherited A's cumulative ($0.606) it would have raised on entry
    # via the pre-flight check. Success here is the isolation proof.
    result_b_tiny = await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="session-B",
    )
    assert result_b_tiny.cumulative_cost_usd == pytest.approx(0.0038, abs=1e-3), (
        "B's tiny call should report its own per-turn cumulative "
        "(~$0.0038), not anything else. The per-session isolation is "
        "implicit: B-tiny did not raise even though A had already "
        f"crossed the ceiling. Got {result_b_tiny.cumulative_cost_usd!r}"
    )


# ---------------------------------------------------------------------------
# 5. Rejoin (same session_id) inherits cumulative
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejoin_same_session_id_inherits_cumulative(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Memory: project_session_id_dropin. ``/play/{date}-{world}-mp`` is
    a deterministic rejoin URL → same session_id → same cumulative
    inheritance. A player who burned $0.40 in session A, disconnects,
    reconnects (same slug → same session_id), and makes another heavy
    call MUST trip the ceiling.

    With ceiling=$0.50 and two heavy calls under the same session_id,
    the second MUST raise — even if a "disconnect" happened in
    between. From the SDK client's perspective, "disconnect" is
    nothing; the next call simply arrives with the same session_id and
    the cumulative is still attached.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_heavy_call(), _heavy_call()])
    client = _build_client(sdk, ceiling=0.50, monkeypatch=monkeypatch)

    # First connection.
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="2026-05-23-glenross-mp",
    )

    # Player "disconnects" — no API-level signal; the client is the
    # same instance (per session_room.py:174-181 the in-process
    # orchestrator outlives WS reconnects).

    # Player rejoins with same slug → same session_id → cumulative
    # carries over. The second heavy call MUST raise.
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="2026-05-23-glenross-mp",
        )


# ---------------------------------------------------------------------------
# 6. cost_ceiling_exceeded event fires once at threshold-cross
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_ceiling_exceeded_watcher_event_shape(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Story §C.3: typed watcher event ``session.cost_ceiling_exceeded``,
    severity=``"error"``, exactly one emit per session at
    threshold-cross. Subsequent refusals do NOT re-emit (the operator
    needs ONE loud alarm, not a stream of duplicates filling the GM
    panel).

    Required fields per story §C.3: session_id, cumulative_cost_usd,
    ceiling_usd, model.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_heavy_call(), _heavy_call(), _heavy_call()])
    client = _build_client(sdk, ceiling=0.50, monkeypatch=monkeypatch)

    # Drive past the ceiling.
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="event-shape-test",
    )
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="event-shape-test",
        )
    # Subsequent refusal — MUST NOT re-emit.
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="event-shape-test",
        )
    await asyncio.sleep(0.05)

    ceiling_events = [
        e for e in sock.events if e.get("event_type") == "session.cost_ceiling_exceeded"
    ]
    assert len(ceiling_events) == 1, (
        "session.cost_ceiling_exceeded MUST fire exactly once per "
        "session at threshold-cross — not once per refused call. Got "
        f"{len(ceiling_events)} events."
    )
    evt = ceiling_events[0]
    assert evt.get("severity") == "error", (
        "session.cost_ceiling_exceeded MUST be severity='error' "
        "(distinct from cost_runaway_suspected's 'warn'). The GM-panel "
        "filter chain routes error to red banner + audible cue. Got "
        f"severity={evt.get('severity')!r}"
    )
    fields = evt.get("fields", {})
    for key in ("session_id", "cumulative_cost_usd", "ceiling_usd", "model"):
        assert key in fields, (
            f"GM panel needs '{key}' on session.cost_ceiling_exceeded "
            f"fields; got fields={list(fields)}."
        )
    assert fields["session_id"] == "event-shape-test"
    assert fields["ceiling_usd"] == pytest.approx(0.50)
    assert fields["cumulative_cost_usd"] >= 0.50


# ---------------------------------------------------------------------------
# 7. cost_running_total fires EVERY successful turn (wiring test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_running_total_fires_every_turn(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Story §C.3 + §Testing wiring requirement: the per-turn
    ``session.cost_running_total`` watcher event MUST fire on every
    successful narrator turn (not just at threshold-cross). Without
    this, the GM-panel live counter has gaps; the dual lie-detector
    pairing with 60-7's narrator.cache.both_writes_fired needs
    continuous data.

    With ceiling=$10.00 (default — won't cross in this test) and three
    healthy calls, exactly three ``session.cost_running_total`` events
    fire — one per turn, NOT one per tool-loop iteration (loop here is
    trivial — one iter per call because we don't script tool_use).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_tiny_call(), _tiny_call(), _tiny_call()])
    # Default ceiling — tiny calls won't approach $10.
    client = AnthropicSdkClient(sdk=sdk, cache_ttl="1h")

    for _ in range(3):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="running-total-test",
        )
    await asyncio.sleep(0.05)

    running_total_events = [
        e for e in sock.events if e.get("event_type") == "session.cost_running_total"
    ]
    assert len(running_total_events) == 3, (
        "session.cost_running_total MUST fire once per successful "
        "turn (3 turns → 3 events). Got "
        f"{len(running_total_events)} events. All events: "
        f"{[e.get('event_type') for e in sock.events]}"
    )
    # Severity must be info (low noise — this is the per-turn pulse).
    for evt in running_total_events:
        assert evt.get("severity") == "info", (
            "session.cost_running_total MUST be severity='info' "
            "(routine per-turn pulse, not an alarm). Got "
            f"severity={evt.get('severity')!r}"
        )

    # Required field shape — cumulative MUST monotonically grow.
    cumulatives = [e["fields"]["cumulative_cost_usd"] for e in running_total_events]
    assert cumulatives == sorted(cumulatives), (
        f"cumulative_cost_usd MUST grow monotonically across turns. "
        f"Got {cumulatives!r}"
    )
    assert all(c > 0 for c in cumulatives), (
        f"Every turn's cumulative_cost_usd MUST be > 0. Got {cumulatives!r}"
    )

    # fraction_used field is the GM-panel "X / $10" denominator.
    for evt in running_total_events:
        fields = evt["fields"]
        assert "fraction_used" in fields, (
            "GM panel needs 'fraction_used' on session.cost_running_total "
            f"fields; got fields={list(fields)}."
        )
        assert "ceiling_usd" in fields
        assert fields["fraction_used"] == pytest.approx(
            fields["cumulative_cost_usd"] / fields["ceiling_usd"]
        )


# ---------------------------------------------------------------------------
# 8. cost_running_total does NOT fire on refused calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_running_total_does_not_fire_on_refused_call(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """A refused call did not consume tokens and did not advance
    cumulative — emitting a running_total event for it would be
    misleading (operator sees a "tick" they didn't pay for).

    Sequence: drive past ceiling. The threshold-cross turn DID consume
    tokens (the crossing iter billed before we noticed), so it CAN
    legitimately emit one running_total OR be elided in favor of the
    cost_ceiling_exceeded event — implementation choice. The next
    refused call (no SDK call, no billing) MUST NOT emit
    running_total.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_heavy_call(), _heavy_call()])
    client = _build_client(sdk, ceiling=0.50, monkeypatch=monkeypatch)

    # Drive past the ceiling.
    await client.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
        session_id="refusal-no-tick",
    )
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="refusal-no-tick",
        )
    await asyncio.sleep(0.05)

    # Snapshot event count after threshold-cross.
    rt_after_cross = [
        e for e in sock.events if e.get("event_type") == "session.cost_running_total"
    ]
    events_before_refuse = len(sock.events)

    # Subsequent refusal — must add zero events.
    with pytest.raises(AnthropicSdkCostCeilingExceeded):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="refusal-no-tick",
        )
    await asyncio.sleep(0.05)

    rt_after_refuse = [
        e for e in sock.events if e.get("event_type") == "session.cost_running_total"
    ]
    assert len(rt_after_refuse) == len(rt_after_cross), (
        "A pure refusal (no SDK call) MUST NOT add a "
        "session.cost_running_total event — the call didn't bill. "
        f"Got {len(rt_after_refuse)} after refuse vs {len(rt_after_cross)} "
        "after threshold-cross."
    )
    assert len(sock.events) == events_before_refuse, (
        "A pure refusal MUST NOT add ANY watcher events (no "
        "running_total, no duplicate ceiling_exceeded, nothing). Got "
        f"{len(sock.events)} total events vs {events_before_refuse} pre-refuse."
    )


# ---------------------------------------------------------------------------
# 9. Non-narrator call (session_id=None) bypasses the tracker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_id_none_bypasses_cumulative_tracker(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Context §C.1: ``None`` session_id bypasses the tracker — for
    non-narrator codepaths (dungeon "curate", future Opus side-calls)
    and tests. They MUST NOT contribute to any session's cumulative,
    and they MUST NOT raise even at high cumulative cost.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_heavy_call(), _heavy_call(), _heavy_call()])
    client = _build_client(sdk, ceiling=0.50, monkeypatch=monkeypatch)

    # Three heavy calls with session_id=None — none should raise even
    # though cumulative-if-tracked would be ~$0.909.
    for _ in range(3):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id=None,
        )


# ---------------------------------------------------------------------------
# 10. Logging: ERROR-level entry on ceiling cross
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ceiling_cross_logs_at_error_level(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Python lang-review #4: error paths use ``logger.error`` with the
    correct severity. Ceiling-cross is a server-side hard-stop (not
    client input validation), so it MUST log at ERROR. Mirrors the
    ``narrator.cost_runaway_suspected`` ERROR line that 61-4 added.
    Project convention: prefix the log line so it's grep-discoverable
    ("session.cost_ceiling_exceeded" — matches the watcher event name).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sdk = _Sdk(responses=[_heavy_call(), _heavy_call()])
    client = _build_client(sdk, ceiling=0.50, monkeypatch=monkeypatch)

    with caplog.at_level(logging.ERROR, logger="sidequest.agents.anthropic_sdk_client"):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
            session_id="log-level-test",
        )
        with pytest.raises(AnthropicSdkCostCeilingExceeded):
            await client.complete_with_tools(
                system_blocks=_system_blocks(),
                messages=_user_msg(),
                tools=_tools_empty(),
                model="claude-sonnet-4-6",
                session_id="log-level-test",
            )

    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and "session.cost_ceiling_exceeded" in r.getMessage()
    ]
    assert len(error_records) >= 1, (
        "Ceiling-cross MUST log at ERROR level with the "
        "'session.cost_ceiling_exceeded' prefix (log + watcher parity, "
        "lang-review #4). Got "
        f"{len(error_records)} ERROR records carrying that prefix. "
        f"All records: {[r.getMessage() for r in caplog.records]}"
    )
