"""Per-session replay retention for ``WatcherHub`` (story 126-23 — server half).

The GM-panel Live view is *unusable during concurrent runs* (sq-playtest
2026-06-19, Keith: "the otel dashboard is also totally fucked"). One root cause
is server-side: the replay ring buffer is a SINGLE shared ``deque(maxlen=2000)``
spanning every session. When a dashboard connects mid-session, the server
replays this shared buffer — but a noisy concurrent session (a headless test
spewing thousands of spans) can evict a quiet driven session's earlier turns
from the shared deque BEFORE the operator ever connects. Selecting the driven
session in the (now session-partitioned) Live view then shows "TURNS 0 /
Waiting for first turn…" — the turns happened, but their buffered history is
gone.

The fix is PER-SESSION retention: one session's event volume must not evict a
concurrent session's buffered history. A larger shared global cap is NOT a fix —
it only postpones the eviction to a longer run. These tests pin the retention
invariant by outcome (the quiet session survives the noisy neighbor); the
mechanism (per-session buffers / per-session budget) is the dev's to choose, but
it must be per-session, not a bigger single deque.

Companion UI half: ``sidequest-ui/.../useLiveSource-session-select.test.tsx``
(operator selection scopes the Live view to the SELECTED session, not the
last-emitting one).
"""

from __future__ import annotations

import asyncio

import pytest

from sidequest.telemetry.watcher_hub import WatcherHub
from tests._helpers.doubles import FakeSocket


@pytest.fixture
async def fresh_hub() -> WatcherHub:
    """A hub bound to the test loop, bypassing the module singleton so buffer
    state never leaks between cases."""
    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())
    return hub


async def _drain(n: int = 40) -> None:
    """Yield enough times for the ``run_coroutine_threadsafe`` broadcast
    callbacks posted by ``publish`` to land in the buffer."""
    for _ in range(n):
        await asyncio.sleep(0)


def _span_close(slug: str, i: int) -> dict:
    """A flat firehose ``agent_span_close`` event for ``slug``."""
    return {
        "timestamp": "t",
        "component": "sidequest-server",
        "event_type": "agent_span_close",
        "severity": "info",
        "session_slug": slug,
        "fields": {"name": "turn.agent_llm.inference", "i": i},
    }


def _turn_complete(slug: str, turn: int) -> dict:
    return {
        "timestamp": "t",
        "component": "orchestrator",
        "event_type": "turn_complete",
        "severity": "info",
        "session_slug": slug,
        "fields": {"turn_id": turn, "agent_name": "narrator"},
    }


@pytest.mark.asyncio
async def test_busy_session_does_not_evict_quiet_session_history(
    fresh_hub: WatcherHub,
) -> None:
    """The realistic repro: a quiet driven session takes a few turns, then a
    concurrent headless test floods the bus past the global cap. The driven
    session's turns MUST survive in replay — they are the whole point of opening
    the panel.

    Today (single shared ``deque(maxlen=2000)``) the 3 driven turns are
    published first, then 3000 noisy events shove them out: replay yields ZERO
    driven turns. That is exactly "TURNS 0 / Waiting for first turn…" under
    concurrency.
    """
    # Quiet driven session: three real turns, emitted first.
    for turn in range(3):
        fresh_hub.publish(_turn_complete("driven", turn))
    # Noisy concurrent session: floods well past the documented 2000 global cap.
    for i in range(3000):
        fresh_hub.publish(_span_close("noisy", i))
    await _drain(n=60)

    sock = FakeSocket()
    await fresh_hub.replay(sock)  # type: ignore[arg-type]

    driven = [e for e in sock.events if e.get("session_slug") == "driven"]
    driven_turn_ids = sorted(
        e["fields"]["turn_id"] for e in driven if e["event_type"] == "turn_complete"
    )
    assert driven_turn_ids == [0, 1, 2], (
        "the quiet driven session's turns were evicted by the noisy neighbor's "
        f"flood — replay retained driven turns {driven_turn_ids}, expected "
        "[0, 1, 2]. Retention must be per-session, not a single shared buffer."
    )


@pytest.mark.asyncio
async def test_quiet_session_survives_when_flood_is_interleaved(
    fresh_hub: WatcherHub,
) -> None:
    """Same invariant, but the noisy session's flood is INTERLEAVED with the
    driven session's turns rather than strictly after — the actual shape of two
    sessions running at once. The driven turns must still all survive replay."""
    for turn in range(4):
        fresh_hub.publish(_turn_complete("driven", turn))
        # ~750 noisy events between each driven turn → 3000 total, past the cap.
        for i in range(750):
            fresh_hub.publish(_span_close("noisy", turn * 750 + i))
    await _drain(n=80)

    sock = FakeSocket()
    await fresh_hub.replay(sock)  # type: ignore[arg-type]

    driven_turn_ids = sorted(
        e["fields"]["turn_id"]
        for e in sock.events
        if e.get("session_slug") == "driven" and e["event_type"] == "turn_complete"
    )
    assert driven_turn_ids == [0, 1, 2, 3], (
        "interleaved noisy traffic evicted the driven session's turns from "
        f"replay — retained {driven_turn_ids}, expected [0, 1, 2, 3]."
    )


@pytest.mark.asyncio
async def test_session_less_infra_events_are_retained_globally(
    fresh_hub: WatcherHub,
) -> None:
    """Session-less infra events (``session_slug=None`` — watcher.connected /
    replay markers) are global and must remain replayable even when a noisy
    session floods the bus; the UI shows them in every session view, so losing
    them to a per-session eviction policy would blind every panel."""
    fresh_hub.publish(
        {
            "timestamp": "t",
            "component": "sidequest-server",
            "event_type": "agent_span_open",
            "severity": "info",
            "session_slug": None,
            "fields": {"name": "watcher.connected"},
        }
    )
    for i in range(3000):
        fresh_hub.publish(_span_close("noisy", i))
    await _drain(n=60)

    sock = FakeSocket()
    await fresh_hub.replay(sock)  # type: ignore[arg-type]

    infra = [e for e in sock.events if e["fields"].get("name") == "watcher.connected"]
    assert len(infra) == 1, (
        "the global session-less infra marker was evicted by the noisy "
        "session's flood — infra events must survive per-session retention."
    )
