"""RED wiring test for Story 65-2 AC3 — ledger write fires from the render path.

The mandatory integration test (CLAUDE.md "Every Test Suite Needs a Wiring
Test" / "No Source-Text Wiring Tests"): drive a real render dispatch through
``_maybe_dispatch_render`` → ``_run_render_inner`` → daemon round-trip, and
prove that when the daemon reply carries an ``r2_key`` (the R2 path), the
handler writes an asset_ledger row and emits an OTEL watcher event.

Contract defined here (Dev implements in GREEN):
  - ``PgSaveRepository.append_asset_ledger(r2_key, asset_type, entity_ref,
    created_turn)`` — parallels the existing ``append_scrapbook_entry``.
  - A watcher event ``state_transition field=asset_ledger op=write`` carrying
    ``r2_key`` / ``asset_type`` (the GM-panel lie detector — see OTEL principle).

Mirrors tests/server/test_render_session_mapping_37_30.py for the daemon +
room + watcher harness. Repository is a MagicMock so the wiring assertion is
"the hook called the store"; tests/persistence/test_pg_asset_ledger.py proves
the store actually persists.
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

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, VisualScene
from sidequest.game.persistence import GameMode
from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData
from sidequest.server.session_room import RoomRegistry
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub


@pytest.fixture
def short_sock() -> Path:
    p = Path(f"/tmp/sq-65-2-{uuid.uuid4().hex[:8]}.sock")
    yield p
    if p.exists():
        p.unlink()


@pytest.fixture
async def bound_hub() -> WatcherHub:
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


def _make_session_data() -> _SessionData:
    from sidequest.game.session import GameSnapshot, TurnManager

    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="Tood's Dome",
        turn_manager=TurnManager(interaction=7),
    )
    return _SessionData(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        player_name="Rux",
        player_id="p-rux",
        snapshot=snap,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(),
        game_slug="test-session-p-rux",
    )


def _make_handler_with_room(sd: _SessionData):
    handler = WebSocketSessionHandler(save_dir=Path("/tmp/never-used"))
    registry = RoomRegistry()
    slug = f"{sd.genre_slug}:{sd.world_slug}:{sd.player_id}"
    room = registry.get_or_create(slug, mode=GameMode.SOLO)
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(registry=registry, socket_id="sock-A", out_queue=queue)
    handler._room = room  # noqa: SLF001
    handler._session_data = sd  # noqa: SLF001
    room.connect(sd.player_id, socket_id="sock-A")
    room.attach_outbound("sock-A", queue)
    return handler, queue


def _eligible(**kwargs) -> NarrationTurnResult:
    kwargs.setdefault("beat_selections", [BeatSelection(actor="t", beat_id="b")])
    return NarrationTurnResult(**kwargs)


def _setup_env(monkeypatch, sock, tmp_path) -> None:
    from sidequest.daemon_client import DaemonClient

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: DaemonClient(socket_path=sock, timeout_seconds=2.0),
    )


# ---------------------------------------------------------------------------
# AC3 — r2_key reply writes a ledger row + emits a watcher event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_with_r2_key_writes_asset_ledger(
    tmp_path: Path, short_sock: Path, monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """A daemon reply carrying r2_key must persist an asset_ledger row via the
    save repository (the runtime→ledger link this whole story exists for)."""
    r2_key = "artifacts/flickering_reach/test-session-p-rux/portrait/deadbeef.png"
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "p.png"),
            "r2_key": r2_key,
            "width": 512,
            "height": 512,
            "elapsed_ms": 30,
        }
    )
    await daemon.start(short_sock)
    _setup_env(monkeypatch, short_sock, tmp_path)

    sd = _make_session_data()
    handler, _queue = _make_handler_with_room(sd)

    result = _eligible(
        narration="Rux looks up.",
        visual_scene=VisualScene(subject="Rux, the kobold scout", tier="portrait"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    await asyncio.sleep(0.2)
    await daemon.stop()

    # Contract: the completion hook calls append_asset_ledger on the repository.
    assert sd.repository.append_asset_ledger.called, (
        "render completion with an r2_key did NOT write an asset_ledger row — "
        "the runtime→save link (the entire point of 65-2) is not wired"
    )
    _args, kwargs = sd.repository.append_asset_ledger.call_args
    call = {**kwargs}
    # Tolerate positional or keyword; pull r2_key from whichever was used.
    passed_key = call.get("r2_key") or (_args[0] if _args else None)
    assert passed_key == r2_key
    assert (call.get("asset_type") or (_args[1] if len(_args) > 1 else None)) == "portrait"


@pytest.mark.asyncio
async def test_render_ledger_write_emits_watcher_event(
    tmp_path: Path, short_sock: Path, monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """The ledger write is a subsystem decision → it must emit an OTEL watcher
    event so the GM panel can verify it fired (CLAUDE.md OTEL principle)."""
    r2_key = "artifacts/flickering_reach/test-session-p-rux/illustration/cafe01.png"
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "s.png"),
            "r2_key": r2_key,
            "width": 1024,
            "height": 768,
            "elapsed_ms": 40,
        }
    )
    await daemon.start(short_sock)
    _setup_env(monkeypatch, short_sock, tmp_path)

    sd = _make_session_data()
    handler, _queue = _make_handler_with_room(sd)
    capture = await _capture(bound_hub)

    result = _eligible(
        narration="The crack yawns.",
        visual_scene=VisualScene(subject="a jagged fissure", tier="scene_illustration"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    await asyncio.sleep(0.2)
    await daemon.stop()

    ledger_events = [
        e
        for e in capture.events
        if e.get("event_type") == "state_transition"
        and e.get("fields", {}).get("field") == "asset_ledger"
        and e.get("fields", {}).get("op") == "write"
    ]
    assert len(ledger_events) == 1, (
        f"expected exactly one asset_ledger write watcher event, got "
        f"{[e.get('fields') for e in capture.events]}"
    )
    assert ledger_events[0]["fields"].get("r2_key") == r2_key


@pytest.mark.asyncio
async def test_render_without_r2_key_writes_no_ledger_row(
    tmp_path: Path, short_sock: Path, monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """Legacy local-tmpdir reply (no r2_key) must write NO ledger row — the
    ledger tracks R2 artifacts only, never local-only renders."""
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "local_only.png"),
            # no r2_key — legacy branch
            "width": 512,
            "height": 512,
            "elapsed_ms": 20,
        }
    )
    await daemon.start(short_sock)
    _setup_env(monkeypatch, short_sock, tmp_path)

    sd = _make_session_data()
    handler, _queue = _make_handler_with_room(sd)

    result = _eligible(
        narration="x",
        visual_scene=VisualScene(subject="x", tier="scene_illustration"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    await asyncio.sleep(0.2)
    await daemon.stop()

    assert not sd.repository.append_asset_ledger.called, (
        "a local-tmpdir render (no r2_key) wrote an asset_ledger row — the "
        "ledger must only track assets that actually reached R2"
    )
