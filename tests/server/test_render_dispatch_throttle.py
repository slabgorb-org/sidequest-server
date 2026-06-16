"""Wiring tests: ImagePacingThrottle is engaged by ``_maybe_dispatch_render``.

Per CLAUDE.md: every test suite needs at least one wiring test. The unit
tests in ``test_image_pacing_throttle.py`` prove the throttle's state
machine in isolation; this file proves the throttle is actually consulted
on the production render dispatch path AND that suppressed renders never
reach the daemon.

Also covers the OTEL emission requirement (ADR-050 + CLAUDE.md OTEL
Observability Principle): both ``allow`` and ``suppress`` branches must
publish a ``render.throttle_decision`` watcher event so the GM panel can
verify the throttle is engaged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, VisualScene
from sidequest.protocol.enums import MessageType
from sidequest.server.image_pacing import ImagePacingThrottle
from sidequest.server.session_handler import (
    WebSocketSessionHandler,
    _SessionData,
)


def _make_eligible_result(**kwargs):
    """Story 45-30: the render trigger policy gates dispatch on structured
    signals. These dispatch-mechanics tests test the wire downstream of
    the policy (URL handling, request payload, broadcasting); they pre-date
    the policy and don't carry signal kwargs. This wrapper injects a default
    BeatSelection so the result classifies as BEAT_FIRE and the test exercises
    its named gate, not the policy.

    Tests asserting the policy itself (test_render_trigger_*) construct
    NarrationTurnResult directly and bypass this helper.
    """
    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult

    if "beat_selections" not in kwargs:
        kwargs["beat_selections"] = [BeatSelection(actor="test", beat_id="dispatch_test")]
    return NarrationTurnResult(**kwargs)


# ---------------------------------------------------------------------------
# Test fixtures — mirror test_render_dispatch.py so the two suites share
# a vocabulary, but kept local so the throttle test is hermetic.
# ---------------------------------------------------------------------------


@pytest.fixture
def short_sock(tmp_path: Path) -> Path:
    del tmp_path
    p = Path(f"/tmp/sq-throttle-test-{uuid.uuid4().hex[:8]}.sock")
    yield p
    if p.exists():
        p.unlink()


class _CountingFakeDaemon:
    """Counts how many ``render`` requests reach the daemon. Used to
    prove that throttled renders never make it to the wire."""

    def __init__(self, reply_payload: dict[str, Any]) -> None:
        self.reply_payload = reply_payload
        self.requests: list[dict[str, Any]] = []
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
            reply = {"id": req.get("id"), "result": self.reply_payload}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def _client_bound_to(path: Path):
    from sidequest.daemon_client import DaemonClient

    return DaemonClient(socket_path=path, timeout_seconds=2.0)


def _make_session_data(
    *,
    throttle: ImagePacingThrottle | None = None,
    player_id: str = "p-1",
) -> _SessionData:
    from sidequest.game.session import GameSnapshot, TurnManager

    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="Tood's Dome",
        turn_manager=TurnManager(interaction=3),
    )
    kwargs: dict[str, Any] = dict(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        player_name="Rux",
        player_id=player_id,
        snapshot=snap,
        store=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(),
        # R2 migration Task 20: production slug-connect always populates
        # game_slug; provide a default so the render dispatcher's
        # session_id propagation has a value to forward.
        game_slug=f"test-session-{player_id}",
    )
    if throttle is not None:
        kwargs["image_pacing_throttle"] = throttle
    return _SessionData(**kwargs)


def _make_handler_with_queue() -> tuple[WebSocketSessionHandler, asyncio.Queue]:
    handler = WebSocketSessionHandler(save_dir=Path("/tmp/never-used"))
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler._out_queue = queue  # noqa: SLF001 — test wiring
    return handler, queue


def _visual_result(subject: str = "a thing") -> NarrationTurnResult:
    return _make_eligible_result(
        narration="...",
        visual_scene=VisualScene(
            subject=subject,
            tier="scene_illustration",
        ),
    )


def _capture_watcher_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def _spy(event: str, payload: dict[str, Any], **kwargs: Any) -> None:
        events.append({"event": event, "payload": payload, "kwargs": kwargs})

    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler._watcher_publish",
        _spy,
    )
    return events


# ---------------------------------------------------------------------------
# Wiring tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_render_passes_throttle_and_reaches_daemon(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: the throttle does NOT block the first render of a session.
    Daemon must see exactly one request."""
    daemon = _CountingFakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "first.png"),
            "width": 512,
            "height": 512,
            "elapsed_ms": 1234,
        }
    )
    await daemon.start(short_sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(short_sock),
    )

    events = _capture_watcher_events(monkeypatch)
    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()  # default-factory throttle (solo, 30s)

    queued = handler._maybe_dispatch_render(sd, _visual_result())  # noqa: SLF001
    assert queued is not None
    assert queued.type == MessageType.RENDER_QUEUED

    image_msg = await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    assert image_msg.type == MessageType.IMAGE
    assert len(daemon.requests) == 1

    # Throttle decision event was published with decision=allow,
    # reason=first_render.
    decisions = [e for e in events if e["payload"].get("op") == "throttle_decision"]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision"] == "allow"
    assert decisions[0]["payload"]["reason"] == "first_render"
    assert decisions[0]["payload"]["render_id"] == queued.payload.render_id
    assert decisions[0]["payload"]["cooldown_seconds"] == 30


@pytest.mark.asyncio
async def test_second_render_within_cooldown_is_suppressed_before_daemon(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing wiring proof: a second render dispatched within
    the cooldown window is suppressed BEFORE the daemon is contacted.
    The daemon's request count stays at 1."""
    daemon = _CountingFakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "first.png"),
            "width": 512,
            "height": 512,
            "elapsed_ms": 1234,
        }
    )
    await daemon.start(short_sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(short_sock),
    )

    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()

    # First render — allowed, hits the daemon.
    queued1 = handler._maybe_dispatch_render(sd, _visual_result("first"))  # noqa: SLF001
    assert queued1 is not None
    await asyncio.wait_for(queue.get(), timeout=2.0)  # drain IMAGE

    # Second render, immediately after — must be suppressed by the
    # 30s solo cooldown.
    queued2 = handler._maybe_dispatch_render(sd, _visual_result("second"))  # noqa: SLF001
    assert queued2 is None, "throttle must suppress second render"

    # Give any erroneously-spawned background coroutine a chance to
    # contact the daemon. None should — but if the throttle is
    # broken we want this test to catch it.
    await asyncio.sleep(0.1)
    await daemon.stop()

    # The daemon saw EXACTLY one request despite two dispatch attempts.
    # This is the load-bearing assertion: throttle suppresses BEFORE
    # the daemon round-trip.
    assert len(daemon.requests) == 1, f"throttle leaked render to daemon: {daemon.requests}"
    # The legacy out_queue must not have received a second IMAGE
    # frame either.
    assert queue.empty()


@pytest.mark.asyncio
async def test_throttle_emits_suppress_decision_otel_event(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OTEL Observability Principle: the GM panel must see a
    ``throttle_decision`` watcher event when a render is suppressed,
    so the operator can verify the throttle actually fired."""
    daemon = _CountingFakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "x.png"),
            "width": 1,
            "height": 1,
            "elapsed_ms": 1,
        }
    )
    await daemon.start(short_sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(short_sock),
    )

    handler, queue = _make_handler_with_queue()
    # Pre-armed throttle: simulate an immediately-prior render so the
    # FIRST dispatch attempt is already suppressed. This isolates the
    # suppress-branch OTEL emission cleanly.
    throttle = ImagePacingThrottle.for_solo()
    throttle.record_render()
    sd = _make_session_data(throttle=throttle)

    events = _capture_watcher_events(monkeypatch)
    queued = handler._maybe_dispatch_render(sd, _visual_result())  # noqa: SLF001
    assert queued is None

    await asyncio.sleep(0.05)
    await daemon.stop()

    # Daemon must NOT have been contacted.
    assert daemon.requests == []
    assert queue.empty()

    # A throttle_decision event with decision=suppress was published.
    decisions = [e for e in events if e["payload"].get("op") == "throttle_decision"]
    assert len(decisions) == 1, f"expected exactly 1 throttle_decision event, got: {events}"
    payload = decisions[0]["payload"]
    assert payload["decision"] == "suppress"
    assert payload["reason"] == "cooldown_active"
    assert payload["field"] == "render"
    assert payload["cooldown_remaining_seconds"] > 0
    assert payload["cooldown_seconds"] == 30
    # Suppressed renders still get a render_id in the OTEL payload so
    # the GM panel can correlate the suppression with the visual_scene
    # the narrator was trying to render.
    assert "render_id" in payload
    assert decisions[0]["kwargs"]["component"] == "render"


@pytest.mark.asyncio
async def test_third_render_after_cooldown_passes(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the cooldown elapses, the next render dispatches normally.
    Forces the throttle's cooldown to a tiny value so the test runs
    in well under a second (real-world 30s default would be glacial)."""
    daemon = _CountingFakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "x.png"),
            "width": 1,
            "height": 1,
            "elapsed_ms": 1,
        }
    )
    await daemon.start(short_sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(short_sock),
    )

    handler, queue = _make_handler_with_queue()
    # 0.1s cooldown — fast enough for a unit test, large enough to
    # observe the suppression in the second dispatch.
    sd = _make_session_data(throttle=ImagePacingThrottle(cooldown_seconds=0))
    sd.image_pacing_throttle.set_cooldown_seconds(1)
    # Cheat: set last_render to ~1.1s in the past via the public API
    # (set the cooldown low and record).
    sd.image_pacing_throttle.set_cooldown_seconds(0)
    handler._maybe_dispatch_render(sd, _visual_result("a"))  # noqa: SLF001
    await asyncio.wait_for(queue.get(), timeout=2.0)

    # Re-enable a 1s cooldown — now any immediate dispatch should be
    # suppressed.
    sd.image_pacing_throttle.set_cooldown_seconds(1)
    assert handler._maybe_dispatch_render(sd, _visual_result("b")) is None  # noqa: SLF001

    # Wait out the cooldown.
    await asyncio.sleep(1.05)
    queued = handler._maybe_dispatch_render(sd, _visual_result("c"))  # noqa: SLF001
    assert queued is not None

    await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    # First and third render reached the daemon; second was suppressed.
    assert len(daemon.requests) == 2
