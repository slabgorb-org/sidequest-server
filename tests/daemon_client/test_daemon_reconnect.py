"""RED tests — Story 153-8 [DAEMON-NO-RECONNECT].

When the media daemon starts **after** the server, the server's daemon client
latches an ``unavailable`` view and never self-heals:

  * ``_maybe_dispatch_render`` skips every render with
    ``reason=daemon_unavailable``;
  * ``lore_embedding`` / ``entity_embedding`` queues accumulate (the playtest
    saw 20-46 pending) and never drain;
  * ``retrieve_lore_context`` returns ``None`` every turn.

The degradation persists until the server is manually restarted with the daemon
already up. That is a **No Silent Fallbacks** violation (server CLAUDE.md): the
client no-ops renders + RAG on every turn and never surfaces a recovery signal.

These tests pin the recovery contract from ADR-131 (daemon<->server out-of-band
liveness heartbeat) and ADR-035 (Unix-socket IPC):

  AC-1 Renders recover without a server restart once the daemon is healthy.
  AC-2 RAG embed/retrieve recover without a server restart; the
       ``daemon_unavailable`` skips cease once the socket is reachable.
  AC-3 The reconnect probe loop is bounded (capped back-off) — it does not
       busy-spin nor block the hot path.
  AC-4 The ``unavailable -> available`` transition is **observable**: a watcher
       event (and log line) fires. The flip is never silent.
  AC-5 The recovery path is driven end-to-end through the *production* code
       against a **real** Unix-domain socket — never by mocking an availability
       flag.

Wire-first (AC-5): every test uses a real ``asyncio`` Unix-socket server
(``_ReconnectFakeDaemon``) that is **absent** at first and started mid-test, so
``DaemonClient.is_available()`` flips on the real filesystem and the production
functions cross the real socket. No availability flag is monkeypatched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from sidequest.daemon_client import DaemonClient
from sidequest.daemon_client.state_mirror import get_mirror
from sidequest.game.lore_embedding import (
    embed_pending_fragments,
    retrieve_lore_context,
)
from sidequest.game.lore_store import LoreCategory, LoreFragment, LoreSource, LoreStore

# ---------------------------------------------------------------------------
# Real-socket harness (AC-5: production path, no mocked flags)
# ---------------------------------------------------------------------------


@pytest.fixture
def short_sock(tmp_path: Path):
    """A short socket path (macOS AF_UNIX sun_path ~104 byte limit; pytest's
    tmp_path under /var/folders blows past it). Yielded **unlinked** so the
    daemon-absent phase is real."""
    del tmp_path  # keep isolation keyed to the fixture, but use /tmp for length
    p = Path(f"/tmp/sq-reconnect-{uuid.uuid4().hex[:8]}.sock")
    yield p
    if p.exists():
        p.unlink()


class _ReconnectFakeDaemon:
    """Minimal real Unix-domain server speaking the daemon's line framing.

    On every accepted connection it first emits one ``heartbeat`` event line
    (so the server's heartbeat listener can record liveness and the mirror can
    recover), then reads one request and replies by method:

      * ``embed``  -> a valid EmbedResponse,
      * ``render`` -> an image result,
      * ``status`` -> an empty status result.

    Tracks ``connection_count`` and fires ``first_connection`` on first accept
    so tests can synchronise deterministically instead of sleeping on a guess.
    """

    def __init__(self) -> None:
        self.connection_count = 0
        self.first_connection = asyncio.Event()
        self.methods_seen: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self._path: Path | None = None

    async def start(self, path: Path) -> None:
        self._path = path
        self._server = await asyncio.start_unix_server(self._handle, path=str(path))

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connection_count += 1
        self.first_connection.set()
        try:
            # ADR-131 liveness: announce a heartbeat on connect.
            heartbeat = {
                "event": "heartbeat",
                "queue": "image",
                "state": "ready",
                "queue_depth": 0,
                "ts_monotonic": 1.0,
            }
            writer.write((json.dumps(heartbeat) + "\n").encode())
            await writer.drain()

            line = await reader.readline()
            if not line:
                return
            req = json.loads(line.decode().strip())
            method = req.get("method", "")
            self.methods_seen.append(method)
            if method == "embed":
                body: dict[str, Any] = {
                    "result": {
                        "embedding": [0.1, 0.2, 0.3],
                        "model": "fake-reconnect",
                        "latency_ms": 1,
                    }
                }
            elif method == "render":
                body = {"result": {"image_url": "/tmp/reconnect.png", "width": 8, "height": 8}}
            else:  # status / anything else
                body = {"result": {"queues": {}}}
            reply = {"id": req.get("id"), **body}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._path is not None and self._path.exists():
            self._path.unlink()


class _SuppressThrottle:
    """Pacing-throttle stub that always suppresses. Lets the render test return
    at the ADR-050 pacing gate — which sits *after* the daemon-availability gate
    — so we exercise availability recovery without driving the full daemon
    round-trip + session-id/R2 keying machinery (unrelated to this story)."""

    cooldown_seconds = 0

    def should_render(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            allowed=False, reason="test_suppressed", cooldown_remaining_seconds=0
        )


def _frag(id_: str, content: str = "content") -> LoreFragment:
    return LoreFragment.new(
        id=id_,
        category=LoreCategory.History,
        content=content,
        source=LoreSource.GenrePack,
    )


async def _capture_watcher_events() -> list[dict]:
    """Subscribe a capture sink to the process-wide watcher hub and return the
    live event list (mutated as events publish)."""
    from sidequest.telemetry.watcher_hub import watcher_hub

    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001 — test wiring
        watcher_hub._subscribers.clear()  # noqa: SLF001

    class _Cap:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.events.append(data)

    cap = _Cap()
    await watcher_hub.subscribe(cap)  # type: ignore[arg-type]
    return cap.events


def _is_recovery_event(e: dict) -> bool:
    """A daemon ``unavailable -> available`` recovery signal (AC-4).

    Tolerant of the exact field/op Dev chooses, but semantically pinned: a
    ``state_transition`` whose op marks a reconnection on a daemon-facing field,
    or a dedicated reconnect event_type. Today **nothing** emits this — that is
    the silent flip the story forbids."""
    op = str(e.get("fields", {}).get("op", "")).lower()
    field = str(e.get("fields", {}).get("field", "")).lower()
    etype = str(e.get("event_type", "")).lower()
    recovery_ops = {"reconnected", "reconnect", "recovered", "available", "restored"}
    daemon_fields = {"daemon", "render", "lore_embedding", "entity_embedding", "lore_retrieval"}
    if "reconnect" in etype or "daemon_recover" in etype:
        return True
    return op in recovery_ops and field in daemon_fields


# ---------------------------------------------------------------------------
# AC-1 / AC-2 / AC-5 — recovery through the real socket, no client recreation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_worker_resumes_after_socket_returns(short_sock: Path) -> None:
    """AC-2 + AC-5: the lore embed worker drains its pending queue once the
    daemon socket becomes reachable — without recreating the store or
    restarting, and driving the real ``client.embed`` socket round-trip."""
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    store.add(_frag("b", "beta"))
    client = DaemonClient(socket_path=short_sock, timeout_seconds=2.0)

    # Phase 1 — daemon absent: worker must skip loudly, queue stays pending.
    skipped = await embed_pending_fragments(store, client=client)
    assert skipped.skipped_daemon_unavailable is True
    assert skipped.embedded == 0
    assert store.fragments["a"].embedding_pending is True
    assert store.fragments["b"].embedding_pending is True

    # Phase 2 — daemon comes up on the SAME path. The same store + a client on
    # the same path must now drain the queue. This is the bug: production stays
    # latched here and never embeds.
    daemon = _ReconnectFakeDaemon()
    await daemon.start(short_sock)
    try:
        recovered = await embed_pending_fragments(
            store, client=DaemonClient(socket_path=short_sock, timeout_seconds=2.0)
        )
    finally:
        await daemon.stop()

    assert recovered.skipped_daemon_unavailable is False, (
        "AC-2: once the socket is reachable the embed worker must resume; "
        "it must not stay latched on the startup-time unavailable view"
    )
    assert recovered.embedded == 2
    assert store.fragments["a"].embedding == [0.1, 0.2, 0.3]
    assert store.fragments["a"].embedding_pending is False
    assert store.fragments["b"].embedding_pending is False


@pytest.mark.asyncio
async def test_retrieve_resumes_after_socket_returns(short_sock: Path) -> None:
    """AC-2 + AC-5: RAG retrieval returns a real lore block once the daemon is
    reachable, after returning ``None`` while it was absent."""
    store = LoreStore()
    store.add(_frag("a", "the sunken cathedral of Vellmoor"))
    # Pre-embed the stored fragment so retrieval has a vector to match against
    # once the daemon is up (retrieval embeds the QUERY and compares).
    daemon = _ReconnectFakeDaemon()
    await daemon.start(short_sock)
    try:
        seeded = await embed_pending_fragments(
            store, client=DaemonClient(socket_path=short_sock, timeout_seconds=2.0)
        )
        assert seeded.embedded == 1
    finally:
        await daemon.stop()

    # Daemon absent again -> retrieval degrades to None (graceful, but degraded).
    absent = await retrieve_lore_context(
        store, "tell me about the cathedral",
        client=DaemonClient(socket_path=short_sock, timeout_seconds=2.0),
    )
    assert absent is None

    # Daemon returns -> retrieval must resume and produce a non-empty block.
    daemon2 = _ReconnectFakeDaemon()
    await daemon2.start(short_sock)
    try:
        recovered = await retrieve_lore_context(
            store, "tell me about the cathedral",
            client=DaemonClient(socket_path=short_sock, timeout_seconds=2.0),
        )
    finally:
        await daemon2.stop()

    assert recovered is not None, (
        "AC-2: retrieval must recover once the daemon socket is reachable again"
    )
    assert "Vellmoor" in recovered or "cathedral" in recovered.lower()


@pytest.mark.asyncio
async def test_render_dispatch_clears_unavailable_after_socket_returns(
    short_sock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1 + AC-5: ``_maybe_dispatch_render`` stops emitting the
    ``daemon_unavailable`` skip once the socket is reachable — driven through
    the real handler against a real socket."""
    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, VisualScene
    from sidequest.game.session import GameSnapshot, TurnManager
    from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    # Point the handler's DaemonClient() construction at the test socket.
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: DaemonClient(socket_path=short_sock, timeout_seconds=2.0),
    )

    # Clean mirror so render_unavailable_pending stays False (we exercise the
    # is_available() socket gate, not the heartbeat-lost gate).
    mirror = get_mirror()
    mirror.clear_for_test()

    from unittest.mock import MagicMock

    def _make_handler() -> WebSocketSessionHandler:
        handler = WebSocketSessionHandler(save_dir=Path("/tmp/never-used"))
        handler._out_queue = asyncio.Queue()  # noqa: SLF001
        return handler

    def _make_sd() -> _SessionData:
        snap = GameSnapshot(
            genre_slug="mutant_wasteland",
            world_slug="flickering_reach",
            location="",
            turn_manager=TurnManager(interaction=4),
        )
        sd = _SessionData(
            genre_slug="mutant_wasteland",
            world_slug="flickering_reach",
            player_name="Rux",
            player_id="player-153-8",
            snapshot=snap,
            repository=MagicMock(),
            dungeon_repository=MagicMock(),
            telemetry_sink=MagicMock(),
            genre_pack=MagicMock(),
            orchestrator=MagicMock(),
        )
        # Suppress at the pacing gate so dispatch returns just past the
        # availability gate (the only gate this AC-1 test exercises).
        sd.image_pacing_throttle = _SuppressThrottle()  # type: ignore[assignment]
        return sd

    def _visual_result() -> NarrationTurnResult:
        return NarrationTurnResult(
            narration="The fissure glows.",
            visual_scene=VisualScene(
                subject="a jagged fissure in red rock",
                tier="scene_illustration",
                mood="ominous",
                tags=["desert"],
            ),
            beat_selections=[BeatSelection(actor="t", beat_id="reconnect_render_test")],
        )

    captured = await _capture_watcher_events()

    def _daemon_unavail_skips() -> list[dict]:
        return [
            e
            for e in captured
            if e.get("event_type") == "state_transition"
            and e.get("fields", {}).get("field") == "render"
            and e.get("fields", {}).get("op") == "skipped"
            and e.get("fields", {}).get("reason") == "daemon_unavailable"
        ]

    # Turn 1 — daemon absent: dispatch must skip with daemon_unavailable.
    handler = _make_handler()
    sd = _make_sd()
    res1 = handler._maybe_dispatch_render(sd, _visual_result())  # noqa: SLF001
    await asyncio.sleep(0.05)
    assert res1 is None
    assert len(_daemon_unavail_skips()) == 1, "turn 1 (daemon down) must skip daemon_unavailable"

    before = len(_daemon_unavail_skips())

    # Turn 2 — daemon up on the same path: dispatch must clear the unavailable
    # gate (no new daemon_unavailable skip).
    daemon = _ReconnectFakeDaemon()
    await daemon.start(short_sock)
    try:
        sd2 = _make_sd()
        handler._maybe_dispatch_render(sd2, _visual_result())  # noqa: SLF001
        await asyncio.sleep(0.1)
    finally:
        await daemon.stop()

    after = len(_daemon_unavail_skips())
    assert after == before, (
        "AC-1: once the daemon is reachable, _maybe_dispatch_render must stop "
        f"emitting daemon_unavailable skips (got {after - before} new skip(s) on "
        "the turn after the socket came up)"
    )


@pytest.mark.asyncio
async def test_heartbeat_listener_clears_unresponsive_after_socket_returns(
    short_sock: Path,
) -> None:
    """AC-2 + AC-5: the ADR-131 heartbeat listener (the designated reconnect
    channel) records liveness once the socket returns, flipping the shared
    mirror out of UNRESPONSIVE — the signal the worker/render gates should
    consult to recover."""
    mirror = get_mirror()
    mirror.clear_for_test()
    assert mirror.is_unresponsive() is True  # cold start: no heartbeat yet

    client = DaemonClient(socket_path=short_sock)
    task = asyncio.create_task(
        client.heartbeat_listener(poll_interval_seconds=0.02, max_idle_seconds=0.2)
    )
    try:
        # Absent for a few probe cycles — mirror stays unresponsive.
        await asyncio.sleep(0.05)
        assert mirror.is_unresponsive() is True

        daemon = _ReconnectFakeDaemon()
        await daemon.start(short_sock)
        try:
            await asyncio.wait_for(daemon.first_connection.wait(), timeout=2.0)
            await asyncio.sleep(0.05)  # let the heartbeat line be recorded
            assert mirror.is_unresponsive() is False, (
                "AC-2: once the listener reconnects to the live socket, the "
                "mirror must leave UNRESPONSIVE — recovery without restart"
            )
        finally:
            await daemon.stop()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ---------------------------------------------------------------------------
# AC-4 — the unavailable -> available flip is observable (NOT silent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnection_emits_observable_watcher_event(short_sock: Path) -> None:
    """AC-4 + AC-5 (RED anchor): when the daemon comes back, the server's
    daemon client must emit an **observable** recovery event (watcher event +
    log) so the GM panel sees the flip. Today the client recovers silently —
    no event marks ``unavailable -> available`` — which is the No-Silent-
    Fallbacks violation this story fixes.

    Driven through the real heartbeat-listener reconnect AND a real worker call
    so the assertion holds whichever seam Dev emits the signal from."""
    mirror = get_mirror()
    mirror.clear_for_test()

    captured = await _capture_watcher_events()

    # Prime an absent-state worker skip so the system has "seen" unavailable.
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    skipped = await embed_pending_fragments(
        store, client=DaemonClient(socket_path=short_sock, timeout_seconds=2.0)
    )
    assert skipped.skipped_daemon_unavailable is True
    assert not any(_is_recovery_event(e) for e in captured), (
        "no recovery event should fire while the daemon is still absent"
    )

    # Reconnect via the listener channel...
    client = DaemonClient(socket_path=short_sock)
    task = asyncio.create_task(
        client.heartbeat_listener(poll_interval_seconds=0.02, max_idle_seconds=0.2)
    )
    daemon = _ReconnectFakeDaemon()
    try:
        await asyncio.sleep(0.04)
        await daemon.start(short_sock)
        await asyncio.wait_for(daemon.first_connection.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
        # ...and drive a worker now that the socket is up (recovery may be
        # signalled from the worker path instead of the listener).
        await embed_pending_fragments(
            store, client=DaemonClient(socket_path=short_sock, timeout_seconds=2.0)
        )
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await daemon.stop()

    recovery = [e for e in captured if _is_recovery_event(e)]
    assert recovery, (
        "AC-4: the daemon-client unavailable->available transition must emit an "
        "observable watcher event (e.g. field='daemon', op='reconnected'); the "
        "recovery must not be a silent flip. None was captured."
    )


# ---------------------------------------------------------------------------
# AC-3 — the reconnect probe loop is bounded and does not busy-spin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_probe_loop_does_not_busy_spin(
    short_sock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: while the daemon socket is absent, the reconnect probe loop must
    sleep a bounded, positive amount between attempts — never a tight spin, and
    never an unbounded/blocking wait on the hot path."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay: float, *a: object, **k: object) -> None:
        delays.append(float(delay))
        if len(delays) >= 3:
            # Break the otherwise-infinite probe loop deterministically once we
            # have enough samples — no wall-clock waiting.
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr("sidequest.daemon_client.client.asyncio.sleep", _recording_sleep)

    client = DaemonClient(socket_path=short_sock)  # socket absent the whole time
    with contextlib.suppress(asyncio.CancelledError):
        await client.heartbeat_listener(poll_interval_seconds=0.5, max_idle_seconds=1.0)

    assert len(delays) >= 3, "the probe loop must wait between attempts, not busy-spin"
    assert all(d > 0 for d in delays), (
        f"AC-3: every reconnect back-off must be a positive sleep (no tight spin); got {delays}"
    )
    # Bounded/capped: a sane reconnect cadence never balloons past a small cap.
    assert max(delays) <= 60.0, (
        f"AC-3: reconnect back-off must be capped (bounded), not unbounded; got {delays}"
    )
