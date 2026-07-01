"""Task 1 — WatcherHub.buffered_events read accessor (RED)."""
from __future__ import annotations

import asyncio

import pytest

from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub


@pytest.fixture
async def bound_hub() -> WatcherHub:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
        watcher_hub._session_buffers.clear()  # noqa: SLF001
        watcher_hub._seq = 0  # noqa: SLF001
    return watcher_hub


def _event(session_slug, event_type):
    return {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "component": "test",
        "event_type": event_type,
        "severity": "info",
        "session_slug": session_slug,
        "fields": {"n": event_type},
    }


@pytest.mark.asyncio
async def test_buffered_events_merges_slug_and_infra_in_seq_order(bound_hub: WatcherHub) -> None:
    bound_hub.publish(_event(None, "infra-a"))
    bound_hub.publish(_event("s1", "s1-a"))
    bound_hub.publish(_event("s2", "s2-a"))
    bound_hub.publish(_event("s1", "s1-b"))
    await asyncio.sleep(0.05)

    events = await bound_hub.buffered_events("s1")
    names = [e["event_type"] for e in events]
    # s1's two events plus the global infra event, in publish order; no s2.
    assert names == ["infra-a", "s1-a", "s1-b"], names


@pytest.mark.asyncio
async def test_buffered_events_unknown_slug_returns_infra_only(bound_hub: WatcherHub) -> None:
    bound_hub.publish(_event(None, "infra-a"))
    bound_hub.publish(_event("s1", "s1-a"))
    await asyncio.sleep(0.05)

    events = await bound_hub.buffered_events("nope")
    assert [e["event_type"] for e in events] == ["infra-a"]
