"""Bug #2b (playtest 2026-04-26): Image broadcast to all players in a room.

Symptom (multiplayer playtest, grimvault, Zanzibar + Gladstone): the
IMAGE landed only on the originating actor's outbound queue — peers
never saw the rendered scene. Shared-world scene/POI/illustration
imagery should be a shared delta: every connected socket in the room
must receive the same IMAGE event.

Root cause: ``_run_render`` looked up *one* socket via
``room.socket_for_player(player_id)`` and pushed the IMAGE onto that
single queue. The fix routes through ``room.broadcast(msg)`` so every
attached outbound queue gets the frame, mirroring the SCRAPBOOK_ENTRY
fan-out pattern.

CLAUDE.md: every test suite needs a wiring test. The room registry,
the broadcast call, and the OTEL completion event are all exercised
end-to-end here.
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


@pytest.fixture
def short_sock(tmp_path: Path) -> Path:
    del tmp_path
    p = Path(f"/tmp/sq-2b-{uuid.uuid4().hex[:8]}.sock")
    yield p
    if p.exists():
        p.unlink()


@pytest.fixture
async def bound_hub() -> WatcherHub:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


class _Cap:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


class _FakeDaemon:
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


def _make_session_data(*, player_id: str, world_slug: str = "grimvault") -> _SessionData:
    from sidequest.game.session import GameSnapshot, TurnManager

    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug=world_slug,
        location="The Throat",
        turn_manager=TurnManager(interaction=3),
    )
    return _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug=world_slug,
        player_name="Zanzibar",
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


def _slug(sd: _SessionData) -> str:
    return f"mp:{sd.genre_slug}:{sd.world_slug}"


def _make_two_player_room(
    sd: _SessionData,
    *,
    actor_socket: str = "sock-zanzibar",
    peer_player: str = "p-gladstone",
    peer_socket: str = "sock-gladstone",
) -> tuple[
    WebSocketSessionHandler,
    RoomRegistry,
    asyncio.Queue,
    asyncio.Queue,
    str,
]:
    """Two players seated in a multiplayer room. Returns the actor's
    handler, registry, both outbound queues, and the room slug."""
    handler = WebSocketSessionHandler(save_dir=Path("/tmp/never-used"))
    registry = RoomRegistry()
    slug = _slug(sd)
    room = registry.get_or_create(slug, mode=GameMode.MULTIPLAYER)
    actor_q: asyncio.Queue[object] = asyncio.Queue()
    peer_q: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(registry=registry, socket_id=actor_socket, out_queue=actor_q)
    handler._room = room  # noqa: SLF001
    handler._session_data = sd  # noqa: SLF001
    room.connect(sd.player_id, socket_id=actor_socket)
    room.attach_outbound(actor_socket, actor_q)
    room.connect(peer_player, socket_id=peer_socket)
    room.attach_outbound(peer_socket, peer_q)
    return handler, registry, actor_q, peer_q, slug


# ----------------------------------------------------------------------
# Bug #2b core fix: IMAGE reaches every connected socket in the room
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_broadcasts_to_all_room_sockets(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grimvault multiplayer regression: shared-world scene imagery
    must reach every connected player. Both Zanzibar's and Gladstone's
    outbound queues should receive the IMAGE — not just the actor's.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "throat.png"),
            "width": 1024,
            "height": 768,
            "elapsed_ms": 800,
        }
    )
    await daemon.start(sock)
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    sd = _make_session_data(player_id="p-zanzibar")
    handler, _registry, actor_q, peer_q, _slug_str = _make_two_player_room(sd)

    result = _make_eligible_result(
        narration="The Throat opens.",
        visual_scene=VisualScene(subject="a corridor of cold stone", tier="scene_illustration"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001

    # Both queues must receive the IMAGE.
    actor_msg = await asyncio.wait_for(actor_q.get(), timeout=2.0)
    peer_msg = await asyncio.wait_for(peer_q.get(), timeout=2.0)
    await daemon.stop()

    assert actor_msg.type == MessageType.IMAGE, (
        "actor never received their own IMAGE — broadcast is excluding "
        "the originator instead of fanning out to all sockets"
    )
    assert peer_msg.type == MessageType.IMAGE, (
        "peer (Gladstone) never received the IMAGE — Bug #2b regression: "
        "shared-world imagery still routing per-player instead of "
        "broadcasting to the room"
    )
    # Same render: both peers see the same URL.
    assert actor_msg.payload.url == peer_msg.payload.url
    assert actor_msg.payload.render_id == peer_msg.payload.render_id


# ----------------------------------------------------------------------
# OTEL lie-detector: completion event records broadcast + recipient count
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_completion_otel_records_broadcast_and_recipients(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_hub: WatcherHub,
) -> None:
    """CLAUDE.md OTEL Observability Principle: the GM panel must be
    able to verify how the IMAGE was delivered. The ``render.completed``
    watcher event must carry ``broadcast`` (bool) and ``recipients``
    (int) so a future regression that re-routes per-player instead of
    broadcasting is visible at a glance.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "scene.png"),
            "width": 1024,
            "height": 768,
            "elapsed_ms": 50,
        }
    )
    await daemon.start(sock)
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    sd = _make_session_data(player_id="p-zanzibar")
    handler, _registry, actor_q, peer_q, _slug_str = _make_two_player_room(sd)

    cap = _Cap()
    await bound_hub.subscribe(cap)  # type: ignore[arg-type]

    result = _make_eligible_result(
        narration="...",
        visual_scene=VisualScene(subject="x", tier="scene_illustration"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001

    # Drain both queues so the broadcast actually completes.
    await asyncio.wait_for(actor_q.get(), timeout=2.0)
    await asyncio.wait_for(peer_q.get(), timeout=2.0)
    await asyncio.sleep(0.05)
    await daemon.stop()

    completed = [
        e
        for e in cap.events
        if e.get("event_type") == "state_transition"
        and e.get("fields", {}).get("field") == "render"
        and e.get("fields", {}).get("op") == "completed"
    ]
    assert len(completed) == 1
    fields = completed[0]["fields"]
    assert fields.get("broadcast") is True, (
        "render.completed event must record broadcast=True for room-"
        "scoped renders so the GM panel can confirm shared-world "
        "fan-out fired"
    )
    assert fields.get("recipients") == 2, (
        f"recipient count wrong — expected 2 (Zanzibar + Gladstone), "
        f"got {fields.get('recipients')!r}. If this drops to 1 the "
        f"broadcast regressed back to per-player delivery"
    )


# ----------------------------------------------------------------------
# Backward-compat: legacy non-room path still single-queues (test/legacy
# only — production never hits this branch since story 37-30).
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_no_room_path_uses_single_queue(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no room context is attached (legacy test path or the
    deprecated genre/world connect), the IMAGE still lands on the
    captured legacy queue. This guards the single-queue fallback so
    existing tests in ``test_render_dispatch.py`` keep working."""
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "legacy.png"),
            "width": 256,
            "height": 256,
            "elapsed_ms": 5,
        }
    )
    await daemon.start(sock)
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/never-used"))
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler._out_queue = queue  # noqa: SLF001
    sd = _make_session_data(player_id="p-solo")

    result = _make_eligible_result(
        narration="...",
        visual_scene=VisualScene(subject="x", tier="scene_illustration"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001

    msg = await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()
    assert msg.type == MessageType.IMAGE
