"""Story 61-followup-D — Absolute input_tokens alarm floor (mitigation B).

The 61-4 I/O fingerprint requires BOTH ``input_tokens > 2 × baseline``
AND ``output_tokens < 50`` — the "hammered-with-no-response" shape. A
40K-in / 800-out call slips past that gate but is still a strong
canary for snapshot bloat / section misroute (3.3× healthy input on
the input axis, regardless of what the model said back).

Mitigation: a new trigger ``input_absolute`` fires on a single SDK
call whose ``input_tokens > _ABSOLUTE_INPUT_TOKENS_FLOOR`` (40_000 per
story body §B), independent of baseline and independent of
``output_tokens``. Slots into the existing priority order:

    io_fingerprint > input_absolute > cost_multiple > cost_absolute

These tests are the RED gate. See
``sprint/context/context-story-61-followup-D.md`` §B for the locked
decisions.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from sidequest.agents.anthropic_sdk_client import (
    _ABSOLUTE_INPUT_TOKENS_FLOOR,
    AnthropicSdkClient,
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


def _build_client(sdk: _Sdk) -> AnthropicSdkClient:
    return AnthropicSdkClient(sdk=sdk, cache_ttl="1h")


# ---------------------------------------------------------------------------
# 1. Constant is the documented 40_000 value
# ---------------------------------------------------------------------------


def test_absolute_input_floor_is_40000() -> None:
    """Drift detector. Story §B locks 40_000 as "halfway between ~20K
    healthy steady-state and 60K runaway fingerprint". A future tweak
    must update the story before passing through this test.
    """
    assert _ABSOLUTE_INPUT_TOKENS_FLOOR == 40_000, (
        f"Story body §B locks the absolute input floor at 40_000. "
        f"Got {_ABSOLUTE_INPUT_TOKENS_FLOOR!r}."
    )


# ---------------------------------------------------------------------------
# 2. Behavioral catch — 50K-in / 800-out fires regardless of output shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_absolute_fires_on_high_output_call(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 40K-in / 800-out shape is exactly the gap (B) is built to
    close. 61-4's io_fingerprint stays silent (output_tokens >= 50);
    cost_multiple stays silent during warmup (the call costs
    ~$0.162 > $0.15 floor — actually that fires; let's use a probe
    sized so cost_multiple is silent too — 50K-in / 800-out costs
    $3/MTok×50K + $15/MTok×800 = $0.150 + $0.012 = $0.162, which is at
    the boundary).

    A cleaner probe: 45K-in / 800-out. Cost $3/MTok×45K + $15/MTok×800
    = $0.135 + $0.012 = $0.147 — BELOW the $0.15 warmup cost floor →
    cost_multiple silent. output ≥ 50 → io_fingerprint silent. The
    ONLY trigger that can speak is ``input_absolute``: 45K > 40K floor.
    Exactly one event, ``trigger="input_absolute"``.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_resp(input_tokens=45_000, output_tokens=800)])
    client = _build_client(sdk)

    with caplog.at_level(logging.ERROR, logger="sidequest.agents.anthropic_sdk_client"):
        await client.complete_with_tools(
            system_blocks=_system_blocks(),
            messages=_user_msg(),
            tools=_tools_empty(),
            model="claude-sonnet-4-6",
        )
    await asyncio.sleep(0.05)

    events = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(events) == 1, (
        "45K-in / 800-out (cost ~$0.147) fires ONLY input_absolute "
        "(cost_multiple silent below $0.15 warmup floor; "
        "io_fingerprint silent at output=800; cost_absolute silent at "
        f"cost=$0.147 < $0.30). Got {len(events)} events: "
        f"{[e['fields'] for e in events]}"
    )
    fields = events[0]["fields"]
    assert fields["trigger"] == "input_absolute", (
        f"Trigger MUST be 'input_absolute'. Got {fields['trigger']!r}. "
        f"All fields: {fields!r}"
    )
    assert fields["input_tokens"] > _ABSOLUTE_INPUT_TOKENS_FLOOR, (
        "Probe must exceed the absolute input floor. Got "
        f"input_tokens={fields['input_tokens']!r}"
    )

    # Log parity — input_absolute is a new trigger but the existing
    # ERROR-line shape must accommodate it (severity stays "warn"
    # per 61-4 decision C).
    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "narrator.cost_runaway_suspected" in r.getMessage()
    ]
    assert len(error_records) == 1, (
        "input_absolute MUST log at ERROR level with the same "
        "narrator.cost_runaway_suspected prefix as other triggers. "
        f"Got {len(error_records)} ERROR records."
    )


# ---------------------------------------------------------------------------
# 3. Independence from baseline — fires on FIRST call with no warmup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_absolute_fires_on_first_call_regardless_of_baseline(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Story §B: "fires regardless of baseline." A 60K-in / 1000-out
    first call (no prior observations at all) MUST trip
    input_absolute on call #1.

    61-4's existing I/O fingerprint also fires on call #1 (against the
    warmup floor) — but only when output_tokens < 50. (B)'s
    contribution is the high-output sibling of that shape: input bloat
    visible even when the model is responsive.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    sdk = _Sdk(responses=[_resp(input_tokens=60_000, output_tokens=1_000)])
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
        "First-call 60K-in / 1000-out MUST fire input_absolute on call "
        "#1 (no prior baseline needed). Got "
        f"{len(events)} events."
    )
    # On a fresh client, warmup=True (deque empty). cost_multiple WILL
    # also fire at 60K-in / 1000-out (~$0.195 > $0.15 warmup floor), so
    # priority order must select the higher-priority trigger.
    # Per story §B, priority is io_fingerprint > input_absolute >
    # cost_multiple > cost_absolute. With output=1000, io_fingerprint
    # is silent (needs output<50) so input_absolute wins.
    assert events[0]["fields"]["trigger"] == "input_absolute", (
        "On a first call where input_absolute AND cost_multiple both "
        "fire, priority order MUST pick input_absolute (higher "
        "priority than cost_multiple per story §B). Got "
        f"trigger={events[0]['fields']['trigger']!r}"
    )
    assert events[0]["fields"]["warmup"] is True, (
        "First call MUST report warmup=True (no prior observations in "
        "the deque). Got "
        f"warmup={events[0]['fields'].get('warmup')!r}"
    )


# ---------------------------------------------------------------------------
# 4. Priority order — io_fingerprint > input_absolute > cost_multiple
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_io_fingerprint_outranks_input_absolute(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """When a call trips BOTH io_fingerprint AND input_absolute (e.g.
    60K-in / 12-out — input > 40K AND output < 50), priority order
    locks io_fingerprint as the reported trigger (it remains the most
    diagnostic shape per 61-4 decision C). Story §B amends 61-4's
    priority order; the existing winner stays the winner.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # 60K-in / 12-out trips io_fingerprint (60K > 2×12K warmup AND
    # output 12 < 50) AND input_absolute (60K > 40K).
    sdk = _Sdk(responses=[_resp(input_tokens=60_000, output_tokens=12)])
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
        "Multi-trigger call MUST collapse to one event. Got "
        f"{len(events)}."
    )
    assert events[0]["fields"]["trigger"] == "io_fingerprint", (
        "Priority order: io_fingerprint > input_absolute. When both "
        f"fire, io_fingerprint wins. Got trigger={events[0]['fields']['trigger']!r}"
    )


# ---------------------------------------------------------------------------
# 5. input_absolute outranks cost_multiple AND cost_absolute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_absolute_outranks_cost_triggers(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """When a call trips input_absolute AND cost_multiple AND
    cost_absolute (e.g. 100K-in / 200-out — cost ~$0.30 to ~$0.31,
    input>40K, output ≥ 50 so io_fingerprint silent), priority order
    selects input_absolute (per story §B amended order).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # 100K-in / 200-out: cost = $3/MTok×100K + $15/MTok×200 = $0.300 +
    # $0.003 = $0.303. > $0.30 absolute floor. > 5 × $0.03 warmup
    # floor = $0.15 → cost_multiple trips. > 40K input → input_absolute
    # trips. output ≥ 50 → io_fingerprint silent.
    sdk = _Sdk(responses=[_resp(input_tokens=100_000, output_tokens=200)])
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
        "Multi-trigger call (input_absolute + cost_multiple + "
        f"cost_absolute) MUST collapse to one event. Got {len(events)}."
    )
    assert events[0]["fields"]["trigger"] == "input_absolute", (
        "Per story §B priority order: input_absolute > cost_multiple "
        "> cost_absolute. When all three fire and io_fingerprint is "
        f"silent (output ≥ 50), input_absolute wins. Got "
        f"trigger={events[0]['fields']['trigger']!r}"
    )


# ---------------------------------------------------------------------------
# 6. Boundary precision — 40_001 fires, 39_999 silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_absolute_boundary_at_40000_strict(
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """Strict ``>`` comparison at the boundary. Story body says
    ``input_tokens > 40000``. A 40_001-token call must fire; a
    39_999-token call must stay silent (all other triggers cleared
    via output 800 and ~12K-out-equivalent cost).

    Even tighter: at exactly 40_000, fires? The story body uses ``>``
    (strict), so 40_000 stays silent. Asserts both edges of the gate.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    # Fresh client for each probe — easier than threading sock cleanup.
    # 39_999: cost ≈ $3/MTok × 39_999 + $15/MTok × 800 = $0.120 + $0.012
    # = $0.132 → below $0.15 warmup floor → cost_multiple silent.
    # input ≤ 40K → input_absolute silent. output 800 → io_fingerprint
    # silent. cost $0.132 < $0.30 → cost_absolute silent. ALL silent.
    sdk_under = _Sdk(responses=[_resp(input_tokens=39_999, output_tokens=800)])
    client_under = _build_client(sdk_under)
    await client_under.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
    )
    await asyncio.sleep(0.05)
    events_under = [
        e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"
    ]
    assert events_under == [], (
        "39_999-in is ≤ 40_000 floor; input_absolute MUST stay silent. "
        f"Got {len(events_under)} events: {[e['fields']['trigger'] for e in events_under]}"
    )

    # 40_001: > 40K → input_absolute trips. Cost ≈ $0.132 keeps
    # cost_multiple/cost_absolute silent; output 800 keeps
    # io_fingerprint silent.
    sock.events.clear()
    sdk_over = _Sdk(responses=[_resp(input_tokens=40_001, output_tokens=800)])
    client_over = _build_client(sdk_over)
    await client_over.complete_with_tools(
        system_blocks=_system_blocks(),
        messages=_user_msg(),
        tools=_tools_empty(),
        model="claude-sonnet-4-6",
    )
    await asyncio.sleep(0.05)
    events_over = [e for e in sock.events if e.get("event_type") == "cost_runaway_suspected"]
    assert len(events_over) == 1 and events_over[0]["fields"]["trigger"] == "input_absolute", (
        "40_001-in MUST trip input_absolute with no other triggers "
        f"firing. Got events={events_over}"
    )
