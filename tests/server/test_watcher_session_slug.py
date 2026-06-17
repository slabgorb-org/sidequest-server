"""Session-slug partitioning of the live watcher stream (OTEL-INSPECTOR fix).

The GM-panel Live view fans out EVERY closed span to EVERY connected
dashboard socket. With two concurrent sessions in one process, world A's
narration/patches bled into world B's Live timeline (sq-playtest 2026-06-16).

The fix tags every live watcher event with a ``session_slug`` partition key —
the save slug (``SessionStateView.session_key`` == ``payload.game_slug``), NOT
the integer ``session_id`` row id (``repository.session_id: int``). These tests
pin:

  * the ContextVar-authoritative / process-global-fallback resolver,
  * ``WatcherSpanProcessor.on_start`` stamping the bound slug on a real span so
    ``on_end`` re-broadcasts it on the ``agent_span_close`` envelope,
  * ``publish_event`` carrying the slug on semantic events,
  * per-context isolation (two sessions don't cross-attribute),
  * the honest no-slug case (context-less / infra spans carry ``None``).
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.telemetry.watcher_hub import (
    WatcherHub,
    bind_session_slug,
    current_session_slug,
    publish_event,
    watcher_hub,
)


class _Capturing:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.events.append(data)


@pytest.fixture(autouse=True)
def _reset_slug_binding():
    """Each test starts and ends with no slug bound (ContextVar + global)."""
    bind_session_slug(None)
    yield
    bind_session_slug(None)


def test_current_session_slug_contextvar_and_fallback() -> None:
    assert current_session_slug() is None
    bind_session_slug("2026-06-16-annees_folles-5cbe9403")
    assert current_session_slug() == "2026-06-16-annees_folles-5cbe9403"


def test_session_slug_isolated_per_context() -> None:
    """Two copied contexts (the asyncio-task model) must not cross-attribute —
    this is the concurrent-session firewall the bug needed."""

    def _bind_and_read(slug: str) -> str | None:
        bind_session_slug(slug)
        return current_session_slug()

    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()
    res_a = ctx_a.run(_bind_and_read, "session-A")
    res_b = ctx_b.run(_bind_and_read, "session-B")
    assert res_a == "session-A"
    assert res_b == "session-B"


@pytest.mark.asyncio
async def test_on_start_stamps_slug_so_span_close_carries_it() -> None:
    """A real span opened while a slug is bound must broadcast that slug on its
    ``agent_span_close`` envelope (on_start stamps → on_end re-broadcasts)."""
    from sidequest.server.watcher import WatcherSpanProcessor

    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())
    sub = _Capturing()
    await hub.subscribe(sub)  # type: ignore[arg-type]

    bind_session_slug("2026-06-16-dust_and_lead-109457a1")

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(hub))
    tracer = provider.get_tracer("test-session-slug")
    with tracer.start_as_current_span("wiring.slug") as span:
        span.set_attribute("probe", "ok")
    await asyncio.sleep(0.05)

    close = [e for e in sub.events if e["fields"].get("name") == "wiring.slug"]
    assert len(close) == 1
    assert close[0]["session_slug"] == "2026-06-16-dust_and_lead-109457a1"
    # The partition key is envelope metadata, not turn content — keep `fields` clean.
    assert "session_slug" not in close[0]["fields"]


@pytest.mark.asyncio
async def test_span_close_carries_none_when_no_slug_bound() -> None:
    """Context-less / infra spans honestly carry ``session_slug=None`` so the
    UI treats them as global (shown in every session), never mis-attributed."""
    from sidequest.server.watcher import WatcherSpanProcessor

    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())
    sub = _Capturing()
    await hub.subscribe(sub)  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(hub))
    tracer = provider.get_tracer("test-no-slug")
    with tracer.start_as_current_span("wiring.noslug"):
        pass
    await asyncio.sleep(0.05)

    close = [e for e in sub.events if e["fields"].get("name") == "wiring.noslug"]
    assert len(close) == 1
    assert close[0]["session_slug"] is None


@pytest.mark.asyncio
async def test_publish_event_carries_session_slug() -> None:
    """Semantic events (turn_complete, state_transition, …) carry the bound
    slug on the envelope so the Live view can scope them too."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    sub = _Capturing()
    await watcher_hub.subscribe(sub)  # type: ignore[arg-type]

    bind_session_slug("2026-06-16-annees_folles-mp-e140a240")
    publish_event("turn_complete", {"turn_number": 3}, component="orchestrator")
    await asyncio.sleep(0.05)

    assert len(sub.events) == 1
    assert sub.events[0]["session_slug"] == "2026-06-16-annees_folles-mp-e140a240"
    # The slug rides the envelope, not `fields` — persisted telemetry stays clean.
    assert "session_slug" not in sub.events[0]["fields"]
