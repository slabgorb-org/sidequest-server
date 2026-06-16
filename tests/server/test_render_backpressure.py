"""RED tests — Story 45-31 — wire-first backpressure boundary (AC3).

The server-side enqueue path (``_maybe_dispatch_render``) consults a
queue-depth counter held on ``_SessionData``. When in-flight renders
exceed the configured threshold (default 3), the server MUST emit a
``render.enqueue.backpressure`` watcher event with
``decision="warn"`` and let the request through.

This is wire-first: no daemon mock at all — we exercise
``handler._maybe_dispatch_render`` and observe the watcher stream.
The test is the explicit boundary contract for AC3.

The backpressure decision is orthogonal to ADR-050 throttle: the
throttle is the *time-based* "do not render this beat" cooldown; the
backpressure check is the *concurrent-load* "the daemon already has 3
renders in flight" loud-warn. Both must be visible in OTEL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, VisualScene
from sidequest.server.session_handler import (
    WebSocketSessionHandler,
    _SessionData,
)


@pytest.fixture
def short_sock(tmp_path: Path) -> Path:
    """Short Unix-socket path (macOS sun_path ~104 bytes)."""
    del tmp_path
    p = Path(f"/tmp/sq-bp-test-{uuid.uuid4().hex[:8]}.sock")
    yield p
    if p.exists():
        p.unlink()


class _BlockingDaemon:
    """A fake daemon that *accepts* requests but never replies until
    released. Lets the server-side dispatcher see N requests in flight
    simultaneously without the rendering layer ever finishing — the
    exact condition the backpressure check is designed to surface."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.release = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None

    async def start(self, path: Path) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(path))

    async def _handle(self, reader, writer) -> None:  # noqa: ANN001
        try:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line.decode())
            self.requests.append(req)
            # Block until the test releases — keeps the request in flight.
            await self.release.wait()
            reply = {
                "id": req.get("id"),
                "result": {
                    "image_url": "/tmp/x.png",
                    "width": 1024,
                    "height": 768,
                    "elapsed_ms": 1,
                },
            }
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def stop(self) -> None:
        self.release.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def _make_session_data() -> _SessionData:
    from unittest.mock import MagicMock

    from sidequest.game.session import GameSnapshot, TurnManager

    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="",
        turn_manager=TurnManager(interaction=1),
    )
    return _SessionData(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        player_name="Rux",
        player_id="player-1",
        snapshot=snap,
        store=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(),
        # R2 migration Task 20: production slug-connect always populates
        # game_slug; tests pre-dating that path get a default so the
        # render dispatcher's session_id propagation has a value.
        game_slug="test-backpressure-session",
    )


def _client_bound_to(path: Path):
    from sidequest.daemon_client import DaemonClient

    return DaemonClient(socket_path=path, timeout_seconds=5.0)


def _make_handler() -> tuple[WebSocketSessionHandler, asyncio.Queue]:
    handler = WebSocketSessionHandler(save_dir=Path("/tmp/never-used"))
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler._out_queue = queue  # noqa: SLF001 — test wiring
    return handler, queue


def _make_visual_result(seq: int) -> NarrationTurnResult:
    # Story 45-30 added a render-trigger policy gate that runs before
    # the backpressure check. Inject a BeatSelection so the result
    # classifies as BEAT_FIRE — the test exercises the backpressure
    # path, not 45-30's policy decision.
    return NarrationTurnResult(
        narration=f"turn {seq}",
        visual_scene=VisualScene(
            subject=f"a scene labelled {seq}",
            tier="scene_illustration",
            mood="neutral",
            tags=[],
        ),
        beat_selections=[BeatSelection(actor="test", beat_id=f"backpressure_test_{seq}")],
    )


async def _capture_watcher_events(loop) -> tuple[list[dict], object]:
    """Subscribe a capture stub to the watcher hub and return its
    event buffer + the stub itself (so the test can unsubscribe)."""
    from sidequest.telemetry.watcher_hub import watcher_hub

    watcher_hub.bind_loop(loop)
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    class _Cap:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.events.append(data)

    cap = _Cap()
    await watcher_hub.subscribe(cap)  # type: ignore[arg-type]
    return cap.events, cap


@pytest.mark.asyncio
async def test_backpressure_warn_event_fires_past_threshold(
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3: depth 3 (default threshold) is fine; the fourth concurrent
    enqueue MUST emit ``render.enqueue.backpressure`` with
    ``decision="warn"``, ``queue_depth=4``, ``threshold=3`` AND still
    proceed (warn mode, not reject)."""
    daemon = _BlockingDaemon()
    await daemon.start(short_sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(short_sock),
    )

    # Force the mirror into READY so the dispatcher does not short-circuit
    # on the unavailable-fallback path. The mirror's exact API is the
    # subject of test_daemon_state_mirror.py — here we just need a clean
    # READY signal so we exercise the backpressure branch in isolation.
    # Use a current monotonic reading so is_unresponsive() reports False.
    import time as _time

    from sidequest.daemon_client.state_mirror import get_mirror

    mirror = get_mirror()
    mirror.clear_for_test()
    mirror.record_heartbeat(
        queue="image",
        state="ready",
        queue_depth=0,
        ts_monotonic=_time.monotonic(),
    )

    captured, _cap = await _capture_watcher_events(asyncio.get_running_loop())

    handler, _queue = _make_handler()
    sd = _make_session_data()
    # Disable the ADR-050 image-pacing throttle for this test — we are
    # exercising the orthogonal backpressure gate; the throttle is a
    # separate, time-based gate covered by test_render_dispatch.py.
    # Without disabling, the second through fourth dispatch calls would
    # be throttle-suppressed and never reach the backpressure check.
    sd.image_pacing_throttle.set_cooldown_seconds(0)

    caplog.set_level(logging.WARNING, logger="sidequest.server.websocket_session_handler")

    # Drive 4 enqueues. The first 3 fill in-flight depth; the 4th must
    # trigger the backpressure warn.
    for n in range(4):
        handler._maybe_dispatch_render(sd, _make_visual_result(n))  # noqa: SLF001

    # Allow the dispatch tasks a chance to actually open sockets (so
    # the daemon registers them as "in flight").
    await asyncio.sleep(0.1)

    backpressure_events = [
        e
        for e in captured
        if e.get("event_type") == "state_transition"
        and e.get("fields", {}).get("field") == "render"
        and e.get("fields", {}).get("op") == "enqueue.backpressure"
    ]

    # AC3: exactly one warn event for the fourth enqueue.
    assert len(backpressure_events) == 1, (
        f"expected exactly 1 render.enqueue.backpressure event for the "
        f"fourth concurrent enqueue, got {len(backpressure_events)}: "
        f"{backpressure_events}"
    )
    fields = backpressure_events[0]["fields"]
    assert fields["decision"] == "warn", (
        "AC3 specifies warn-mode at the default threshold; reject is "
        "only conservative tunable, never the default behavior."
    )
    assert fields["queue_depth"] == 4
    assert fields["threshold"] == 3
    assert "turn_number" in fields
    assert "player_id" in fields

    # AC3: the fourth call still proceeded — the daemon saw four
    # requests, not three.
    daemon.release.set()
    await asyncio.sleep(0.1)
    await daemon.stop()

    assert len(daemon.requests) >= 4, (
        f"warn mode must let the request through; got {len(daemon.requests)} requests at the daemon"
    )

    # AC3: a WARN-level log line accompanies the backpressure event.
    backpressure_logs = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and "backpressure" in rec.getMessage().lower()
    ]
    assert backpressure_logs, (
        "AC3 specifies a loud WARN log alongside the OTEL event — "
        "no log line found containing 'backpressure'"
    )


@pytest.mark.asyncio
async def test_backpressure_below_threshold_emits_no_event(
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative: depth ≤ threshold must NOT emit a backpressure event.
    Catches a regression where the threshold is off-by-one or where
    every enqueue emits the warn unconditionally."""
    daemon = _BlockingDaemon()
    await daemon.start(short_sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(short_sock),
    )

    import time as _time

    from sidequest.daemon_client.state_mirror import get_mirror

    mirror = get_mirror()
    mirror.clear_for_test()
    mirror.record_heartbeat(
        queue="image",
        state="ready",
        queue_depth=0,
        ts_monotonic=_time.monotonic(),
    )

    captured, _cap = await _capture_watcher_events(asyncio.get_running_loop())

    handler, _queue = _make_handler()
    sd = _make_session_data()
    sd.image_pacing_throttle.set_cooldown_seconds(0)

    # Drive only 3 enqueues — exactly at threshold, must not warn.
    for n in range(3):
        handler._maybe_dispatch_render(sd, _make_visual_result(n))  # noqa: SLF001

    await asyncio.sleep(0.1)
    daemon.release.set()
    await daemon.stop()

    backpressure_events = [
        e
        for e in captured
        if e.get("event_type") == "state_transition"
        and e.get("fields", {}).get("field") == "render"
        and e.get("fields", {}).get("op") == "enqueue.backpressure"
    ]
    assert backpressure_events == [], (
        f"depth at the threshold must not warn — got {len(backpressure_events)} events"
    )
