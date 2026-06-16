"""Unit tests for ``WatcherHub`` ring-buffer replay.

When a new dashboard connects mid-session, ``WatcherHub.replay`` must
deliver every buffered event in publish order before any live event
arrives. Without this, a refresh during a session resets every panel
to zero and counters never catch up — see playtest 2026-05-09 finding
"Dashboard has no replay/persistence".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from sidequest.telemetry.watcher_hub import WatcherHub


class _FakeSocket:
    """Records every event a hub broadcast or replay sent us."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


class _DyingSocket:
    """Records events until ``die_after`` reached, then raises on send."""

    def __init__(self, die_after: int) -> None:
        self.events: list[dict[str, Any]] = []
        self.die_after = die_after

    async def send_json(self, data: dict[str, Any]) -> None:
        if len(self.events) >= self.die_after:
            raise RuntimeError("connection lost")
        self.events.append(data)


@pytest.fixture
async def fresh_hub() -> WatcherHub:
    """A hub bound to the test loop. Bypassing the module singleton
    keeps each test isolated — buffer state never leaks between cases."""
    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())
    return hub


async def _drain(n: int = 5) -> None:
    """Yield enough times for ``run_coroutine_threadsafe`` callbacks
    posted by ``publish`` to land in the buffer."""
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_replay_sends_all_buffered_events(fresh_hub: WatcherHub) -> None:
    """A new subscriber's ``replay`` returns every event published so
    far, in publish order."""
    for i in range(5):
        fresh_hub.publish({"event_type": "test", "fields": {"i": i}})
    await _drain()
    sock = _FakeSocket()
    count = await fresh_hub.replay(sock)  # type: ignore[arg-type]
    assert count == 5
    assert [e["fields"]["i"] for e in sock.events] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_replay_buffer_caps_at_maxlen(fresh_hub: WatcherHub) -> None:
    """At ``maxlen=2000`` the deque drops oldest; replay sees only the
    most-recent 2000 events."""
    for i in range(2001):
        fresh_hub.publish({"event_type": "test", "fields": {"i": i}})
    await _drain(n=20)
    sock = _FakeSocket()
    count = await fresh_hub.replay(sock)  # type: ignore[arg-type]
    assert count == 2000
    # First buffered i is 1, last is 2000 — i=0 was evicted.
    assert sock.events[0]["fields"]["i"] == 1
    assert sock.events[-1]["fields"]["i"] == 2000


@pytest.mark.asyncio
async def test_replay_returns_partial_count_on_disconnect(
    fresh_hub: WatcherHub,
) -> None:
    """A subscriber drop mid-replay aborts cleanly with the partial
    count rather than propagating an exception."""
    for i in range(10):
        fresh_hub.publish({"event_type": "test", "fields": {"i": i}})
    await _drain()
    sock = _DyingSocket(die_after=3)
    count = await fresh_hub.replay(sock)  # type: ignore[arg-type]
    assert count == 3
    assert len(sock.events) == 3


@pytest.mark.asyncio
async def test_buffer_holds_serialized_form_not_raw_event(
    fresh_hub: WatcherHub,
) -> None:
    """The buffer must hold the JSON-safe serialized event so a future
    replay cannot reintroduce a ``TypeError`` from a non-stdlib value
    that was coerced during the original broadcast (e.g. datetime,
    Pydantic newtype)."""
    fresh_hub.publish(
        {
            "event_type": "test",
            "fields": {"when": datetime(2026, 5, 9, 14, 0, 0, tzinfo=UTC)},
        }
    )
    await _drain()
    sock = _FakeSocket()
    await fresh_hub.replay(sock)  # type: ignore[arg-type]
    assert len(sock.events) == 1
    # ``datetime`` should have been coerced to an ISO string by
    # ``_json_default`` during the original broadcast — the buffer
    # carries that coerced form, not the raw datetime object.
    assert isinstance(sock.events[0]["fields"]["when"], str)


@pytest.mark.asyncio
async def test_stats_reports_buffer_depth(fresh_hub: WatcherHub) -> None:
    """``stats`` exposes ``buffered`` so the dashboard can show the
    backlog depth — without it, an empty replay is indistinguishable
    from a wired-but-quiet bus."""
    for i in range(3):
        fresh_hub.publish({"event_type": "test", "fields": {"i": i}})
    await _drain()
    stats = fresh_hub.stats()
    assert stats["buffered"] == 3


@pytest.mark.asyncio
async def test_replay_does_not_drain_buffer(fresh_hub: WatcherHub) -> None:
    """``replay`` is non-destructive: a second subscriber connecting
    afterward also sees every event. Required for multi-tab sessions
    where each player's tab opens its own watcher socket."""
    for i in range(4):
        fresh_hub.publish({"event_type": "test", "fields": {"i": i}})
    await _drain()
    first = _FakeSocket()
    second = _FakeSocket()
    await fresh_hub.replay(first)  # type: ignore[arg-type]
    await fresh_hub.replay(second)  # type: ignore[arg-type]
    assert len(first.events) == 4
    assert len(second.events) == 4
