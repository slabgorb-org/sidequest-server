"""Story 125-9 — ``SIDEQUEST_NO_WATCHER`` skips watcher wiring at startup (RED).

Behavior-level wiring test (CLAUDE.md: No Source-Text Wiring Tests). We do NOT grep
``app.py`` for the call site — we drive the REAL ``create_app()`` lifespan via
``with TestClient(...)`` (the same full-startup path
``test_startup_schema_guard_wiring`` exercises) and assert the watcher hub's event
loop is — or is not — bound depending on the flag.

The bound loop is the exact runtime state that gates ``WatcherHub.publish``: with no
loop bound, every publish drops, so harness test-run activity never reaches the live
GM dashboard. We assert BOTH the runtime state AND the behavioral consequence (a
publish does not broadcast), so this is not a structural-only check.

Two halves of Keith's "Both" decision:
  * flag SET   -> watcher NOT wired (the default isolation for harness runs).
  * flag UNSET -> watcher fully LIVE (the opt-in guarantee that a separate-port
    harness server, launched WITHOUT the flag, still emits real OTEL spans). Disabling
    observability must be explicit, never the default.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sidequest.game import db_pool
from sidequest.server.app import create_app
from sidequest.telemetry import watcher_hub as wh


@pytest.fixture
def reset_hub_loop() -> Iterator[None]:
    """The hub is a builtins-pinned singleton (survives reloads AND persists across
    tests). Start each test from an unbound baseline and restore it afterwards so we
    never leak a closed loop into another suite. Persistence sinks are neutralized so
    a publish needs no DB."""
    wh.watcher_hub._loop = None  # noqa: SLF001
    wh.bind_event_store(None)
    wh.bind_session_slug(None)
    try:
        yield
    finally:
        wh.watcher_hub._loop = None  # noqa: SLF001
        wh.bind_event_store(None)
        wh.bind_session_slug(None)


@pytest.fixture
def pg_env(monkeypatch: pytest.MonkeyPatch, migrated_db: str) -> Iterator[None]:
    """Point the process pool at a freshly-migrated throwaway DB so the real lifespan
    startup (which opens + schema-checks the pool) can boot. Mirrors the 126-34
    isolation suite's ``pool`` fixture."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    try:
        yield
    finally:
        db_pool.close_pool()


def test_no_watcher_flag_skips_loop_binding_at_startup(
    monkeypatch: pytest.MonkeyPatch, reset_hub_loop: None, pg_env: None
) -> None:
    monkeypatch.setenv("SIDEQUEST_NO_WATCHER", "1")

    with TestClient(create_app()):
        # Startup ran. With the flag set, _wire_watcher must NOT bind the loop.
        assert wh.watcher_hub._loop is None, (  # noqa: SLF001
            "SIDEQUEST_NO_WATCHER=1 must skip watcher_hub.bind_loop at startup so "
            "harness test-run sessions never register with the operator's live hub"
        )
        # Behavioral consequence (the lie-detector): a publish cannot broadcast.
        before = wh.watcher_hub.stats()
        wh.publish_event("turn_complete", {"turn_id": 1})
        after = wh.watcher_hub.stats()
        assert after["published"] == before["published"], (
            "no event may be broadcast while the watcher is disabled — test-run "
            "activity must stay off the live GM dashboard"
        )
        assert after["dropped"] >= before["dropped"] + 1


def test_watcher_live_by_default_when_flag_unset(
    monkeypatch: pytest.MonkeyPatch, reset_hub_loop: None, pg_env: None
) -> None:
    """Opt-in control (Keith's 'Both', path b): with the flag UNSET the watcher is
    fully wired, so a separate-port harness server gets a real, live hub and real OTEL
    spans. Disabling the watcher must be EXPLICIT — never the default."""
    monkeypatch.delenv("SIDEQUEST_NO_WATCHER", raising=False)

    with TestClient(create_app()):
        loop = wh.watcher_hub._loop  # noqa: SLF001
        assert loop is not None and not loop.is_closed(), (
            "with SIDEQUEST_NO_WATCHER unset the watcher must be LIVE — bind_loop runs "
            "at startup so the GM dashboard sees events"
        )
        before = wh.watcher_hub.stats()
        wh.publish_event("turn_complete", {"turn_id": 2})
        after = wh.watcher_hub.stats()
        assert after["published"] >= before["published"] + 1, (
            "a live watcher must broadcast a published event, not drop it"
        )
