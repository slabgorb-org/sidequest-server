"""Story 37-30 — Session-to-job mapping for render_broadcast.

The bug: ``_run_render`` captures the per-socket outbound queue in its
closure at dispatch time. If the player reconnects on a new socket while
the render is in flight (or disconnects entirely), the IMAGE message
lands in a queue nobody is reading and is silently dropped — observed in
playtest 3 (2026-04-19) as renders that "never arrived" and missing
portrait initials.

The fix: at dispatch, record ``render_id → (room_slug, player_id)``.
At completion, look up the *current* outbound queue via
``RoomRegistry.get(slug).socket_for_player(player_id)`` →
``room.queue_for_socket(socket_id)``. If the player is no longer
connected, emit a ``state_transition op=session_not_found`` watcher
event so the GM panel sees the drop instead of it being silent.

These tests are RED today: the routing-via-registry behavior does not
exist yet, and the dispatched watcher event lacks ``player_id`` /
``room_slug`` fields.

See ``.session/37-30-session.md`` for the four design deviations from
spec (job_id vs render_id naming, watcher events vs real OTEL spans,
request/response vs broadcast architecture, AC-4 scoping).
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
from sidequest.game.persistence import GameMode
from sidequest.protocol.enums import MessageType
from sidequest.server.session_handler import (
    WebSocketSessionHandler,
    _SessionData,
)
from sidequest.server.session_room import RoomRegistry
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub


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
    from sidequest.agents.orchestrator import BeatSelection

    if "beat_selections" not in kwargs:
        kwargs["beat_selections"] = [BeatSelection(actor="test", beat_id="dispatch_test")]
    return NarrationTurnResult(**kwargs)


# ----------------------------------------------------------------------
# Shared fixtures and helpers
# ----------------------------------------------------------------------


@pytest.fixture
def short_sock(tmp_path: Path) -> Path:
    del tmp_path
    p = Path(f"/tmp/sq-37-30-{uuid.uuid4().hex[:8]}.sock")
    yield p
    if p.exists():
        p.unlink()


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the singleton hub to this loop and clear stale subscribers."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


class _FakeSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


async def _capture(hub: WatcherHub) -> _FakeSocket:
    sock = _FakeSocket()
    await hub.subscribe(sock)  # type: ignore[arg-type]
    return sock


class _FakeDaemon:
    """Unix-domain echo server matching the daemon protocol."""

    def __init__(self, reply_payload: dict[str, Any], delay_seconds: float = 0.0) -> None:
        self.reply_payload = reply_payload
        self.requests: list[dict[str, Any]] = []
        self.delay = delay_seconds
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
            if self.delay > 0:
                await asyncio.sleep(self.delay)
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


def _make_session_data(
    *, player_id: str = "p-rux", world_slug: str = "flickering_reach"
) -> _SessionData:
    from sidequest.game.session import GameSnapshot, TurnManager

    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug=world_slug,
        location="Tood's Dome — Nest Crack",
        turn_manager=TurnManager(interaction=3),
    )
    return _SessionData(
        genre_slug="mutant_wasteland",
        world_slug=world_slug,
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


def _client_bound_to(path: Path):
    from sidequest.daemon_client import DaemonClient

    return DaemonClient(socket_path=path, timeout_seconds=2.0)


def _slug(sd: _SessionData) -> str:
    """The room slug used by ws_endpoint to register a SessionRoom.

    Tests construct this directly from session data so they can exercise
    the registry lookup path without booting the full /ws machinery.
    """
    return f"{sd.genre_slug}:{sd.world_slug}:{sd.player_id}"


def _make_handler_with_room(
    sd: _SessionData,
    *,
    socket_id: str = "sock-A",
) -> tuple[WebSocketSessionHandler, RoomRegistry, asyncio.Queue, str]:
    """Build a handler wired into a real RoomRegistry — the production
    code path that ws_endpoint takes — so the registry-lookup fix has
    something live to look up against."""
    handler = WebSocketSessionHandler(save_dir=Path("/tmp/never-used"))
    registry = RoomRegistry()
    slug = _slug(sd)
    room = registry.get_or_create(slug, mode=GameMode.SOLO)
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(registry=registry, socket_id=socket_id, out_queue=queue)
    handler._room = room  # noqa: SLF001 — _room set in slug-connect branch normally
    handler._session_data = sd  # noqa: SLF001
    room.connect(sd.player_id, socket_id=socket_id)
    room.attach_outbound(socket_id, queue)
    return handler, registry, queue, slug


# ----------------------------------------------------------------------
# AC-1: dispatch records mapping; watcher event carries player_id and slug
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_dispatch_event_includes_player_and_room_slug(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """The render dispatch watcher event must carry enough identity to
    route the completion: ``render_id``, ``player_id``, and ``room_slug``.

    Today the event has render_id + tier + subject + turn_number but no
    player_id or room_slug, so a future GM-panel correlator (or the
    completion handler) can't tie a dispatched render back to a session.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "render_a.png"),
            "width": 1024,
            "height": 768,
            "elapsed_ms": 100,
        }
    )
    await daemon.start(sock)
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    sd = _make_session_data()
    handler, _registry, _queue, slug = _make_handler_with_room(sd)
    capture = await _capture(bound_hub)

    result = _make_eligible_result(
        narration="The crack yawns open.",
        visual_scene=VisualScene(subject="a jagged fissure", tier="scene_illustration"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001

    await asyncio.sleep(0.1)
    await daemon.stop()

    dispatched = [
        e
        for e in capture.events
        if e["event_type"] == "state_transition"
        and e["fields"].get("field") == "render"
        and e["fields"].get("op") == "dispatched"
    ]
    assert len(dispatched) == 1, (
        f"expected 1 render.dispatched watcher event, got {len(dispatched)}: "
        f"{[e['fields'] for e in capture.events]}"
    )
    fields = dispatched[0]["fields"]
    assert fields.get("player_id") == sd.player_id, (
        "render dispatch event missing player_id — completion handler "
        "can't route the IMAGE back to the right player"
    )
    assert fields.get("room_slug") == slug, (
        "render dispatch event missing room_slug — multi-room servers "
        "can't disambiguate the session"
    )


# ----------------------------------------------------------------------
# AC-2: routing via registry — IMAGE lands on the *current* live queue
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_completion_routes_to_current_queue_after_reconnect(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing wiring test.

    Scenario: render dispatched on socket A, player reconnects on socket
    B before the daemon replies, daemon replies. The IMAGE message must
    land on socket B's queue (the live one), not socket A's (orphaned).

    Today this fails: ``_run_render`` captured queue A in its closure,
    so the IMAGE goes to the dead queue and the player on socket B never
    sees their portrait.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "render_b.png"),
            "width": 512,
            "height": 512,
            "elapsed_ms": 50,
        },
        delay_seconds=0.15,
    )
    await daemon.start(sock)
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    sd = _make_session_data()
    handler, registry, queue_a, slug = _make_handler_with_room(sd, socket_id="sock-A")

    result = _make_eligible_result(
        narration="Portrait pose.",
        visual_scene=VisualScene(subject="Rux's gaunt face", tier="portrait"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001

    # Simulate reconnect: player drops socket A and connects on socket B
    # *before* the daemon's 150 ms reply arrives.
    room = registry.get(slug)
    assert room is not None
    room.detach_outbound("sock-A")
    room.disconnect(socket_id="sock-A")
    queue_b: asyncio.Queue[object] = asyncio.Queue()
    room.connect(sd.player_id, socket_id="sock-B")
    room.attach_outbound("sock-B", queue_b)

    # Daemon reply arrives. The fix must look up the *current* queue
    # (queue_b), not the captured queue_a.
    await asyncio.sleep(0.5)
    await daemon.stop()

    assert queue_a.empty(), (
        "IMAGE landed on the orphaned socket-A queue — render delivery "
        "is still using a closure-captured queue instead of the live "
        "registry lookup"
    )
    assert not queue_b.empty(), (
        "IMAGE never landed on the live socket-B queue — render lost "
        "across reconnect (the playtest 3 symptom)"
    )
    msg = await asyncio.wait_for(queue_b.get(), timeout=0.1)
    assert msg.type == MessageType.IMAGE
    assert msg.player_id == sd.player_id


# ----------------------------------------------------------------------
# AC-3: no silent drop — disconnected player → watcher warning
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_completion_emits_session_not_found_when_disconnected(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """If the player has fully disconnected by the time the daemon
    replies, the render must not be silently dropped. The completion
    path emits a ``state_transition op=session_not_found`` watcher event
    so the GM panel can see why the image vanished.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "render_c.png"),
            "width": 512,
            "height": 512,
            "elapsed_ms": 20,
        },
        delay_seconds=0.15,
    )
    await daemon.start(sock)
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    sd = _make_session_data()
    handler, registry, queue_a, slug = _make_handler_with_room(sd)
    capture = await _capture(bound_hub)

    result = _make_eligible_result(
        narration="x",
        visual_scene=VisualScene(subject="x", tier="scene_illustration"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001

    # Player disconnects entirely before the daemon reply.
    room = registry.get(slug)
    assert room is not None
    room.detach_outbound("sock-A")
    room.disconnect(socket_id="sock-A")

    await asyncio.sleep(0.5)
    await daemon.stop()

    not_found = [
        e
        for e in capture.events
        if e["event_type"] == "state_transition"
        and e["fields"].get("field") == "render"
        and e["fields"].get("op") == "session_not_found"
    ]
    assert len(not_found) == 1, (
        "render completion silently dropped the IMAGE for a "
        "disconnected player — must emit op=session_not_found"
    )
    fields = not_found[0]["fields"]
    assert fields.get("player_id") == sd.player_id
    assert fields.get("room_slug") == slug
    assert fields.get("render_id"), "missing render_id on drop event"
    assert not_found[0]["severity"] == "warning", (
        "session_not_found must be a warning, not info — operators need it visible in the GM panel"
    )

    # The orphaned queue stays empty; nothing should have been put there.
    assert queue_a.empty(), (
        "IMAGE landed on a detached queue — the silent-drop path is still active"
    )


# ----------------------------------------------------------------------
# AC-4: portrait renders pass character name through to the daemon
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portrait_render_params_include_character_name(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A portrait-tier render must pass the character's name (or initials)
    through to the daemon so the generated image can include the name
    overlay. Today the dispatch params dict carries ``subject`` (free
    text) and no ``subject_name``, so the daemon's portrait-card
    composer has nothing to draw initials from.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "p.png"),
            "width": 512,
            "height": 512,
            "elapsed_ms": 10,
        }
    )
    await daemon.start(sock)
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    sd = _make_session_data(player_id="p-rux")
    handler, _registry, _queue, _slug = _make_handler_with_room(sd)
    result = _make_eligible_result(
        narration="Rux looks up.",
        visual_scene=VisualScene(subject="Rux, the kobold scout", tier="portrait"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001

    await asyncio.sleep(0.2)
    await daemon.stop()

    assert len(daemon.requests) == 1
    params = daemon.requests[0]["params"]
    assert params.get("subject_name") == "Rux", (
        "portrait dispatch must pass subject_name=Rux so the daemon's "
        "card composer can render the name/initials overlay (AC-4 — "
        "missing portrait initials in playtest 3)"
    )


# ----------------------------------------------------------------------
# AC-5: end-to-end happy path through the registry
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_render_routes_through_registry_on_happy_path(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """End-to-end: enqueue a render with a connected player, daemon
    replies, IMAGE lands on the registered queue, and no
    session_not_found warning fires.

    This is the happy-path counterpart to the reconnect test: it proves
    the registry-lookup change doesn't regress the common case where
    nothing changes mid-render.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "render_ok.png"),
            "width": 1024,
            "height": 768,
            "elapsed_ms": 4200,
        }
    )
    await daemon.start(sock)
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    sd = _make_session_data()
    handler, _registry, queue, slug = _make_handler_with_room(sd)
    capture = await _capture(bound_hub)

    result = _make_eligible_result(
        narration="The crack yawns open.",
        visual_scene=VisualScene(subject="a jagged fissure", tier="scene_illustration"),
    )
    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is not None
    assert queued.type == MessageType.RENDER_QUEUED

    image_msg = await asyncio.wait_for(queue.get(), timeout=2.0)
    # Give the background _run_render task time to emit the completed watcher event
    await asyncio.sleep(0.05)
    await daemon.stop()

    assert image_msg.type == MessageType.IMAGE
    assert image_msg.player_id == sd.player_id
    assert image_msg.payload.render_id == queued.payload.render_id

    # Watcher trail: dispatched + completed, no session_not_found.
    render_events = [
        e
        for e in capture.events
        if e["event_type"] == "state_transition" and e["fields"].get("field") == "render"
    ]
    ops = {e["fields"].get("op") for e in render_events}
    assert "dispatched" in ops
    assert "completed" in ops
    assert "session_not_found" not in ops, (
        "happy-path render emitted session_not_found — registry "
        "lookup is misfiring on connected players"
    )

    # The completion event should also carry the mapping fields so the
    # GM panel can correlate dispatch and completion across reconnects.
    completed = [e for e in render_events if e["fields"].get("op") == "completed"]
    assert len(completed) == 1
    cfields = completed[0]["fields"]
    assert cfields.get("player_id") == sd.player_id
    assert cfields.get("room_slug") == slug


# ----------------------------------------------------------------------
# Lang-review #6: meaningful assertions self-check is implicit — every
# test above asserts a *value* (not just truthiness) and a specific
# negative ("not in ops", "queue_a.empty()") — see TEA Assessment.
# ----------------------------------------------------------------------
