"""Wiring test: session handler dispatches a render when the narrator
flags a visual scene.

Exercises :meth:`WebSocketSessionHandler._maybe_dispatch_render` against
a real ``DaemonClient`` talking to an in-process Unix-socket fake
daemon. Asserts:

1. A ``RENDER_QUEUED`` message is returned in the turn's outbound frames.
2. The render task fires a request to the daemon.
3. The daemon's reply is translated into an ``IMAGE`` message posted to
   the connection's outbound queue.
4. With the feature flag off, no render fires and no RENDER_QUEUED ships.

This is the wiring test CLAUDE.md requires — the unit tests in
``test_daemon_client.py`` cover the client alone; this one proves the
full pipeline from narration-result → daemon → UI message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, VisualScene
from sidequest.protocol.enums import MessageType
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


@pytest.fixture
def short_sock(tmp_path: Path) -> Path:
    """Short Unix-socket path (macOS caps sun_path ~104 bytes; pytest's
    tmp_path blows past it). Cleaned up after the test."""
    del tmp_path
    p = Path(f"/tmp/sq-render-test-{uuid.uuid4().hex[:8]}.sock")
    yield p
    if p.exists():
        p.unlink()


class _FakeDaemon:
    """Unix-domain echo server matching the daemon protocol."""

    def __init__(self, reply_payload: dict[str, Any]) -> None:
        self.reply_payload = reply_payload
        self.requests: list[dict[str, Any]] = []
        self._server: asyncio.AbstractServer | None = None
        self._ready = asyncio.Event()

    async def start(self, path: Path) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(path))
        self._ready.set()

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


def _make_session_data(player_id: str = "p-1") -> _SessionData:
    from unittest.mock import MagicMock

    from sidequest.game.session import GameSnapshot, TurnManager

    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        turn_manager=TurnManager(interaction=3),
    )
    snap.character_locations["Rux"] = "Tood's Dome — Nest Crack"
    sd = _SessionData(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        player_name="Rux",
        player_id=player_id,
        snapshot=snap,
        store=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(),
        # R2 migration Task 20: production slug-connect always populates
        # game_slug; tests pre-dating that path get a default so the render
        # dispatcher's session_id propagation has a value to forward.
        game_slug=f"test-session-{player_id}",
    )
    return sd


def _make_session_data_with_pc(
    player_name: str = "Rux",
    player_id: str = "p-1",
    *,
    race: str = "human",
    char_class: str = "ranger",
) -> _SessionData:
    """Variant of ``_make_session_data`` with a real Character on the snapshot
    so the portrait-dispatch path can project descriptor fields. The seat
    map is keyed by player_id → character.core.name so the resolver lands
    on this character (not the legacy first-PC path)."""
    from unittest.mock import MagicMock

    from sidequest.game.character import Character, CreatureCore
    from sidequest.game.session import GameSnapshot, TurnManager

    character = Character(
        core=CreatureCore(
            name=player_name,
            description="A weathered traveler.",
            personality="Quiet, observant.",
        ),
        backstory="Born in the dust under the lamppost towns.",
        char_class=char_class,
        race=race,
        pronouns="they/them",
    )
    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        turn_manager=TurnManager(interaction=3),
        characters=[character],
        player_seats={player_id: player_name},
    )
    snap.character_locations[player_name] = "Tood's Dome — Nest Crack"
    sd = _SessionData(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        player_name=player_name,
        player_id=player_id,
        snapshot=snap,
        store=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(),
        # R2 migration Task 20: see _make_session_data.
        game_slug=f"test-session-{player_id}",
    )
    return sd


def _make_handler_with_queue() -> tuple[WebSocketSessionHandler, asyncio.Queue]:
    handler = WebSocketSessionHandler(save_dir=Path("/tmp/never-used"))
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler._out_queue = queue  # noqa: SLF001 — test wiring
    return handler, queue


@pytest.mark.asyncio
async def test_render_dispatch_fires_daemon_and_enqueues_image(
    tmp_path: Path, short_sock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "render_abc.png"),
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

    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    result = _make_eligible_result(
        narration="The crack yawns open.",
        visual_scene=VisualScene(
            subject="a jagged fissure in red rock",
            tier="scene_illustration",
            mood="ominous",
            tags=["desert", "ruin"],
        ),
    )

    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is not None
    assert queued.type == MessageType.RENDER_QUEUED
    render_id = queued.payload.render_id
    assert len(render_id) == 12

    # Drain the background render coroutine.
    image_msg = await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    assert image_msg.type == MessageType.IMAGE
    assert image_msg.payload.render_id == render_id
    assert image_msg.payload.url == "/renders/render_abc.png"
    assert image_msg.payload.width == 1024
    assert image_msg.payload.tier == "scene_illustration"

    # Daemon saw the full request with narrator-derived fields.
    assert len(daemon.requests) == 1
    req = daemon.requests[0]
    assert req["method"] == "render"
    assert req["params"]["subject"] == "a jagged fissure in red rock"
    assert req["params"]["tier"] == "scene_illustration"
    assert req["params"]["mood"] == "ominous"
    assert req["params"]["tags"] == ["desert", "ruin"]
    # Playtest 2026-04-30 fix: the server used to forward
    # ``sd.snapshot.location`` here as free-form narrator prose
    # ("Tood's Dome — Nest Crack"). The daemon's PromptComposer expects a
    # ``where:<scope>/<slug>`` PlaceCatalog ref; ``PlaceCatalog.get`` now
    # raises a ValueError on the scheme prefix check, which (pre-fix)
    # bubbled out of ``_handle_client`` and EOFed the socket. Until the
    # server tracks slug-aware locations, scene_illustration must send
    # ``location=""`` so the daemon's by-design "transient location" path
    # engages and the action prose carries the setting.
    assert req["params"]["location"] == "", (
        "scene_illustration must NOT forward free-form snapshot.location "
        "as a place ref — daemon's PlaceCatalog rejects non-`where:` refs"
    )
    assert req["params"]["genre"] == "mutant_wasteland"
    # Slice 1 of catalog-injected compose wiring + Bug #2a (playtest
    # 2026-04-26): server must send ``world`` so the daemon's compose path
    # can scope catalogs (CharacterCatalog, PlaceCatalog, StyleCatalog) per
    # (genre, world) AND engage the world-scoped visual_style. Without it,
    # the compose gate short-circuits and falls back to the legacy
    # prose-subject prompt — a silent styleless fallback.
    assert req["params"]["world"] == "flickering_reach", (
        "render request missing ``world`` — daemon's PromptComposer gate "
        "in sidequest_daemon/media/daemon.py will short-circuit and fall "
        "back to a styleless raw prompt"
    )


@pytest.mark.asyncio
async def test_render_dispatch_otel_includes_genre_and_world(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug #2a lie-detector (CLAUDE.md OTEL Observability Principle).

    The GM panel needs to verify that the daemon's PromptComposer will
    actually engage. The dispatched watcher event must carry both
    ``genre`` and ``world`` so the panel can spot a styleless fallback
    before the rendered image arrives.
    """
    import asyncio as _asyncio

    from sidequest.telemetry.watcher_hub import watcher_hub

    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "render_x.png"),
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

    # Bind the watcher hub to this loop and capture events.
    watcher_hub.bind_loop(_asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    class _Cap:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.events.append(data)

    cap = _Cap()
    await watcher_hub.subscribe(cap)  # type: ignore[arg-type]

    handler, _queue = _make_handler_with_queue()
    sd = _make_session_data()
    result = _make_eligible_result(
        narration="...",
        visual_scene=VisualScene(subject="x", tier="scene_illustration"),
    )
    handler._maybe_dispatch_render(sd, result)  # noqa: SLF001

    await _asyncio.sleep(0.1)
    await daemon.stop()

    dispatched = [
        e
        for e in cap.events
        if e.get("event_type") == "state_transition"
        and e.get("fields", {}).get("field") == "render"
        and e.get("fields", {}).get("op") == "dispatched"
    ]
    assert len(dispatched) == 1, (
        f"expected exactly 1 render.dispatched event, got {len(dispatched)}"
    )
    fields = dispatched[0]["fields"]
    assert fields.get("genre") == "mutant_wasteland", (
        "render.dispatched event missing ``genre`` — GM panel can't "
        "verify the daemon's PromptComposer will engage genre style"
    )
    assert fields.get("world") == "flickering_reach", (
        "render.dispatched event missing ``world`` — GM panel can't "
        "spot a styleless silent fallback before the image arrives"
    )


@pytest.mark.asyncio
async def test_portrait_dispatch_emits_structured_pc_ref_and_descriptor(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 2 of catalog-injected compose wiring (paired with sidequest-daemon
    PR registering runtime PCs from descriptor blobs).

    For portrait dispatches the server must emit a structured ``pc:<slug>``
    ref AND a descriptor blob so the daemon can ``add_pc`` into its
    CharacterCatalog without a disk-side portrait_manifest entry. Without
    this projection the daemon's compose path eats a CatalogMissError on
    every portrait and falls through to the prose-subject prompt — i.e.
    slice 2 is dead until both sides ship.

    Pins:
      * ``params["characters"] == ["pc:<slug>"]`` — the structured ref the
        daemon's ``build_render_target`` consumes for tier=PORTRAIT.
      * ``params["pc_descriptor"]`` carries id/appearance/default_pose/culture
        the daemon's ``CharacterTokens`` constructor needs.
      * Slug is lowercase, whitespace→``_``, drops punctuation — same rule
        as ``catalogs._slugify_name`` so PCs registered at runtime collide-
        check cleanly with NPCs loaded from disk.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "portrait_xyz.png"),
            "width": 768,
            "height": 1024,
            "elapsed_ms": 3100,
        }
    )
    await daemon.start(sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    handler, queue = _make_handler_with_queue()
    sd = _make_session_data_with_pc(player_name="Roxie Two-Tongues")
    result = _make_eligible_result(
        narration="Roxie's silhouette against the sodium lamps.",
        visual_scene=VisualScene(
            subject="a wiry rover lit by sodium lamps",
            tier="portrait",
            mood="moody",
            tags=["nightside"],
        ),
    )

    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is not None

    # Drain the background render so the daemon receives the params.
    await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    assert len(daemon.requests) == 1
    params = daemon.requests[0]["params"]
    assert params["tier"] == "portrait"
    # Structured PC ref — slugified to mirror catalogs._slugify_name.
    assert params["characters"] == ["pc:roxie_two-tongues"]
    descriptor = params["pc_descriptor"]
    assert descriptor["id"] == "roxie_two-tongues"
    assert descriptor["appearance"], "appearance prose must be non-empty"
    # ``human ranger`` — descriptor's appearance prose is built from
    # (race, char_class) — that's what the daemon will replicate to every LOD.
    assert "human" in descriptor["appearance"].lower()
    assert "ranger" in descriptor["appearance"].lower()
    # Slug rule: lowercase + collapse whitespace + drop punctuation except _/-.
    # An apostrophe (e.g. "O'Connor") would be dropped, but here the test
    # name has only the hyphen which is preserved.
    assert "-" in descriptor["id"]


@pytest.mark.asyncio
async def test_scene_illustration_dispatch_uses_characters_key_not_participants(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playtest 2026-04-30 contract drift: scene_illustration dispatch
    used to set ``params["participants"] = [pc:<slug>]`` but the daemon's
    ``build_cue_from_params`` only reads ``params.get("characters", [])``
    — so the PC ref never reached the composer's casting plan and every
    illustration rendered with no participant tokens. The portrait
    branch already used ``characters``; both branches must use the same
    on-the-wire field.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "scene_xyz.png"),
            "width": 1024,
            "height": 768,
            "elapsed_ms": 4500,
        }
    )
    await daemon.start(sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    handler, queue = _make_handler_with_queue()
    sd = _make_session_data_with_pc(player_name="Hokulea")
    result = _make_eligible_result(
        narration="Hokulea pries open the sprung locker.",
        visual_scene=VisualScene(
            subject="a sprung exploration locker in red corridor light",
            tier="scene_illustration",
            mood="urgent",
            tags=["ship-interior"],
        ),
    )

    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is not None

    await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    assert len(daemon.requests) == 1
    params = daemon.requests[0]["params"]
    assert params["tier"] == "scene_illustration"
    assert params["characters"] == ["pc:hokulea"], (
        "scene_illustration must send PC ref under ``characters`` (the "
        "key the daemon reads), not ``participants``. Pre-fix, the daemon "
        "saw an empty participants list and the casting layer was empty."
    )
    assert "participants" not in params, (
        "legacy ``participants`` key must not be sent — daemon doesn't "
        "read it and leaving it in is a contract trap"
    )
    # And the location-fix from the same playtest: scene_illustration
    # must NOT forward the snapshot's free-form prose location.
    assert params["location"] == ""
    # Descriptor still flows through so the daemon can register the PC.
    assert params["pc_descriptor"]["id"] == "hokulea"


@pytest.mark.asyncio
async def test_portrait_dispatch_omits_descriptor_when_no_character_seated(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the snapshot has no character to project (e.g. an early portrait
    fired before chargen confirmation), the structured PC ref still ships —
    the daemon's safe wrapper will catalog-miss + fall back. The descriptor
    is omitted so we don't ship empty-prose tokens that would degrade the
    fallback prompt."""
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "portrait_empty.png"),
            "width": 768,
            "height": 1024,
            "elapsed_ms": 3000,
        }
    )
    await daemon.start(sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    handler, queue = _make_handler_with_queue()
    sd = _make_session_data(player_id="p-2")  # no characters on snapshot
    result = _make_eligible_result(
        narration="...",
        visual_scene=VisualScene(subject="a face in shadow", tier="portrait"),
    )

    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is not None
    await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    params = daemon.requests[0]["params"]
    # Ref still ships — daemon's try_compose handles the catalog miss.
    assert params["characters"] == ["pc:rux"]
    assert "pc_descriptor" not in params


@pytest.mark.asyncio
async def test_landscape_dispatch_strips_free_form_location(
    tmp_path: Path, short_sock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Playtest 2026-05-02 fix: ``tier=landscape`` was the last dispatch
    branch still forwarding ``sd.snapshot.location`` as free-form prose.
    Every Coyote Star landscape render replied COMPOSE_FAILED because the
    daemon's PlaceCatalog requires a ``where:<slug>`` ref. The fix hoists
    the sanitize step above the per-tier branches so every tier honours
    the documented contract: only ``where:`` refs survive into the
    daemon request; anything else is dropped to ``""`` so the daemon's
    by-design "transient location" path engages.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "render_landscape.png"),
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

    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()  # snapshot.location = "Tood's Dome — Nest Crack"
    result = _make_eligible_result(
        narration="The dome opens.",
        visual_scene=VisualScene(
            subject="a high desert dome under brass-orrery skies",
            tier="landscape",
            mood="vast",
            tags=["desert", "sky"],
        ),
    )

    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is not None

    await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    assert len(daemon.requests) == 1
    params = daemon.requests[0]["params"]
    assert params["tier"] == "landscape"
    assert params["location"] == "", (
        "landscape must NOT forward free-form snapshot.location as a place "
        "ref — daemon's PlaceCatalog rejects non-`where:` refs and replies "
        "COMPOSE_FAILED. Pre-fix every landscape render in single-player "
        "Coyote Star failed this way."
    )


@pytest.mark.asyncio
async def test_render_dispatch_includes_session_id_for_r2_keying(
    tmp_path: Path, short_sock: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 migration Task 20 — server propagates session_id to daemon params.

    The R2 artifact path layout is
    ``artifacts/<world>/<session>/<kind>/<sha>.<ext>``. The daemon's zimage
    worker reads ``session_id`` from the inbound params dict and falls back
    to the literal string ``"unknown"`` when missing — defeating per-session
    bucketing in production. This wiring test pins that the server's render
    dispatch carries ``session_id`` (sourced from the bound game slug, which
    is the canonical session identifier for the room) so the daemon's R2
    upload keys correctly without needing the fallback.

    Pins:
      * ``params["session_id"]`` is present in the request to the daemon.
      * Its value is the session's ``game_slug`` — the same identifier
        already persisted in the SQLite store and used everywhere else the
        server needs to name the session.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "render_session.png"),
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

    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    sd.game_slug = "abc123-test-session"
    result = _make_eligible_result(
        narration="...",
        visual_scene=VisualScene(
            subject="a jagged fissure in red rock",
            tier="scene_illustration",
            mood="ominous",
            tags=["desert"],
        ),
    )

    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is not None

    await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    assert len(daemon.requests) == 1
    params = daemon.requests[0]["params"]
    assert "session_id" in params, (
        "render request missing ``session_id`` — daemon's R2 upload will "
        "fall back to the literal ``unknown`` segment, breaking the "
        "per-session artifact bucketing the R2 migration spec promises"
    )
    assert params["session_id"] == "abc123-test-session", (
        f"render request session_id={params['session_id']!r} should match "
        "the bound game_slug — the canonical session identifier the rest "
        "of the server uses to name the session"
    )


@pytest.mark.asyncio
async def test_render_skipped_when_flag_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SIDEQUEST_RENDER_ENABLED", raising=False)
    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    result = _make_eligible_result(
        narration="...",
        visual_scene=VisualScene(subject="x", tier="scene_illustration"),
    )
    assert handler._maybe_dispatch_render(sd, result) is None  # noqa: SLF001
    assert queue.empty()


@pytest.mark.asyncio
async def test_render_skipped_when_no_visual_scene(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    result = _make_eligible_result(narration="flat text turn")
    assert handler._maybe_dispatch_render(sd, result) is None  # noqa: SLF001
    assert queue.empty()


@pytest.mark.asyncio
async def test_render_skipped_when_daemon_socket_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    missing = tmp_path / "never-created.sock"
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(missing),
    )
    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    result = _make_eligible_result(
        narration="...",
        visual_scene=VisualScene(subject="x", tier="scene_illustration"),
    )
    assert handler._maybe_dispatch_render(sd, result) is None  # noqa: SLF001
    assert queue.empty()


def _client_bound_to(path: Path):
    """Return a DaemonClient fixed on a given socket path — used to swap
    the default-constructed client in the handler."""
    from sidequest.daemon_client import DaemonClient

    return DaemonClient(socket_path=path, timeout_seconds=2.0)


@pytest.mark.asyncio
async def test_render_dispatch_self_heals_after_daemon_restart(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S4-BUG wiring test (CLAUDE.md mandate).

    Simulates the playtest 2026-04-26 failure: server boots with the OLD
    daemon's tmp dir mounted. Daemon restarts; its NEW tmp dir is
    different. A render-completed reply now lands with image_url under
    the new dir.

    Without the fix: ``_render_url_from_path`` falls through (path not
    under SIDEQUEST_OUTPUT_DIR), the IMAGE message ships an absolute
    filesystem path, the UI 404s on it.

    With the fix: ``ensure_render_mount`` registers the new dir on the
    live mount and the IMAGE message ships a clean ``/renders/...`` URL
    that an HTTP GET against the live app actually serves.
    """
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from fastapi.testclient import TestClient

    from sidequest.server import render_mounts

    # OLD daemon dir (env points here at startup; it's empty/stale).
    old_dir = tmp_path / "sq-daemon-OLD"
    old_dir.mkdir()
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(old_dir))

    # NEW daemon dir (post-restart) with a real image file.
    new_dir = tmp_path / "sq-daemon-NEW" / "zimage"
    new_dir.mkdir(parents=True)
    image_file = new_dir / "render_post_restart.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\nactual-bytes")

    # Daemon reply uses the NEW path.
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(image_file),
            "width": 512,
            "height": 512,
            "elapsed_ms": 1234,
        }
    )
    await daemon.start(short_sock)

    # Build a minimal app that mirrors create_app's mount + active-app
    # registration so the heal code path can find the live mount.
    app = FastAPI()
    app.mount(
        "/renders",
        StaticFiles(directory=str(old_dir)),
        name="render_assets",
    )
    render_mounts.set_active_app(app)
    try:
        monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
        monkeypatch.setattr(
            "sidequest.server.websocket_session_handler.DaemonClient",
            lambda: _client_bound_to(short_sock),
        )

        handler, queue = _make_handler_with_queue()
        sd = _make_session_data()
        result = _make_eligible_result(
            narration="The new tmpdir's pixels.",
            visual_scene=VisualScene(
                subject="post-restart scene",
                tier="scene_illustration",
            ),
        )

        queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
        assert queued is not None
        image_msg = await asyncio.wait_for(queue.get(), timeout=2.0)
        await daemon.stop()

        assert image_msg.type == MessageType.IMAGE
        # The URL must be a clean /renders/* path (NOT an absolute
        # filesystem path with the leading slash of /var or /private).
        url = image_msg.payload.url
        assert url.startswith("/renders/"), (
            f"expected /renders/* URL, got absolute path: {url!r} — "
            f"the self-healing mount didn't fire"
        )
        assert url.endswith("render_post_restart.png")

        # Wiring proof: an HTTP GET against the live app actually serves
        # the file from the NEW dir.
        client = TestClient(app)
        resp = client.get(url)
        assert resp.status_code == 200, (
            f"GET {url} returned {resp.status_code}: healed mount didn't make the file reachable"
        )
        assert resp.content == b"\x89PNG\r\n\x1a\nactual-bytes"
    finally:
        render_mounts.reset_for_app(app)
        render_mounts.set_active_app(None)


# ---------------------------------------------------------------------------
# Story 45-30: Render trigger policy contract + OTEL render.trigger reasons
#
# The pre-story behaviour gates render dispatch on the narrator's optional
# `visual_scene` block — a single `visual is None or not subject.strip()`
# check at the top of `_maybe_dispatch_render`. Felix's 71-turn Playtest 3
# (2026-04-19) exposed the consequence: 6–8 renders out of 71 turns, with
# selection driven by Claude's improvisation rather than narrative weight.
#
# These tests drive `_maybe_dispatch_render` end-to-end and assert that
# the trigger decision is governed by an explicit five-value policy whose
# reason lands on a `render.trigger` watcher event. The wire under test is
# the call site in WebSocketSessionHandler — not the pure classifier
# (covered by tests/server/test_render_trigger_policy.py).
#
# AC mapping:
#   AC1/AC3 (positive reasons) → test_render_trigger_emits_<reason>_*
#   AC2/AC3 (banter/none_policy) → test_render_trigger_banter_emits_none_policy_skip
#   AC2 (priority order)         → test_render_trigger_priority_beat_fire_over_npc_intro
#   AC3 SPAN_ROUTES registration → test_render_trigger_span_route_registered
#   AC6 Felix-shape replay       → test_felix_shape_replay_eight_turn_sequence
# ---------------------------------------------------------------------------


def _capture_watcher_events() -> tuple[object, list[dict]]:
    """Subscribe a fake socket to ``watcher_hub`` for the current loop and
    return ``(capture, events)`` so a test can inspect emitted events."""
    import asyncio as _asyncio

    from sidequest.telemetry.watcher_hub import watcher_hub

    watcher_hub.bind_loop(_asyncio.get_running_loop())

    class _Cap:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.events.append(data)

    return _Cap(), []  # caller uses .events on the cap object


def _watcher_events_matching(
    cap: object, *, field: str | None = None, op: str | None = None
) -> list[dict]:
    """Filter ``cap.events`` for the typed watcher event fields the GM
    panel parses. Per ADR-031 every routed span lands as a
    ``state_transition`` whose ``fields`` carries the route's component
    metadata."""
    out: list[dict] = []
    for ev in cap.events:  # type: ignore[attr-defined]
        if ev.get("event_type") != "state_transition":
            continue
        fields = ev.get("fields", {}) or {}
        if field is not None and fields.get("field") != field:
            continue
        if op is not None and fields.get("op") != op:
            continue
        out.append(ev)
    return out


async def _bind_capture() -> object:
    """Bind a ``_Cap`` to ``watcher_hub`` and return it."""
    import asyncio as _asyncio

    from sidequest.telemetry.watcher_hub import watcher_hub

    watcher_hub.bind_loop(_asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    class _Cap:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.events.append(data)

    cap = _Cap()
    await watcher_hub.subscribe(cap)  # type: ignore[arg-type]
    return cap


def _visual_scene_for_turn() -> VisualScene:
    """A canonical scene_illustration ``VisualScene`` — used so the
    pre-story short-circuit (`visual is None or not subject.strip()`)
    cannot mask the new policy gate. Tests still expect dispatch only
    when a positive trigger reason fires, even with a visual_scene
    present."""
    return VisualScene(
        subject="Felix at the lip of the crater",
        tier="scene_illustration",
        mood="watchful",
        tags=["wasteland"],
    )


def _visual_scene_or_none(*, with_visual: bool) -> VisualScene | None:
    return _visual_scene_for_turn() if with_visual else None


@pytest.mark.asyncio
async def test_render_trigger_span_route_registered() -> None:
    """AC3: ``render.trigger`` is a real route on ``SPAN_ROUTES`` so the
    GM panel parses it as a typed watcher event (component=render).
    Without registration the span fires but never reaches the panel —
    ADR-031 requires every render decision to surface."""
    from sidequest.telemetry.spans._core import SPAN_ROUTES

    assert "render.trigger" in SPAN_ROUTES, (
        "render.trigger missing from SPAN_ROUTES — GM panel cannot "
        "render the trigger reason and the lie-detector loses its "
        "primary render signal (CLAUDE.md OTEL Observability Principle)"
    )
    route = SPAN_ROUTES["render.trigger"]
    assert route.event_type == "state_transition"
    assert route.component == "render"

    assert "render.policy_skip" in SPAN_ROUTES, (
        "render.policy_skip missing from SPAN_ROUTES — the GM panel "
        "needs a focused filter for the silent-by-design banter case"
    )
    skip_route = SPAN_ROUTES["render.policy_skip"]
    assert skip_route.event_type == "state_transition"
    assert skip_route.component == "render"


def _reason_drives_dispatch_params() -> list[tuple[str, dict]]:
    """Yields (reason_name, NarrationTurnResult kwargs) — one positive
    fixture per trigger reason. Each kwargs dict produces a result whose
    ONLY positive signal is the named reason; the others are absent so
    the priority-ordering test below has clean signal isolation.

    The fixture deliberately sets ``visual_scene`` so this test cannot be
    silently passed by the legacy ``visual is None`` short-circuit — the
    new policy must actively classify the reason."""
    from sidequest.agents.orchestrator import (
        BeatSelection,
        NpcMention,
    )

    # The shared snapshot location stays "Tood's Dome — Nest Crack".
    # SCENE_CHANGE flips it via the dispatch-time pre-snapshot location
    # (passed to the classifier), not by mutating the snapshot here.
    return [
        (
            "beat_fire",
            {
                "narration": "Felix triggers the trap.",
                "visual_scene": _visual_scene_for_turn(),
                "beat_selections": [BeatSelection(actor="Felix", beat_id="trap_sprung")],
            },
        ),
        (
            "scene_change",
            {
                "narration": "The dust gives way to glassed sand.",
                "visual_scene": _visual_scene_for_turn(),
                "location": "The Glass Flats",  # != snapshot.location
            },
        ),
        (
            "npc_intro",
            {
                "narration": "Across the rubble, a stranger.",
                "visual_scene": _visual_scene_for_turn(),
                "npcs_present": [NpcMention(name="Sallow Dree", is_new=True)],
            },
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,result_kwargs",
    _reason_drives_dispatch_params(),
    ids=[r[0] for r in _reason_drives_dispatch_params()],
)
async def test_render_trigger_emits_reason_and_dispatches(
    reason: str,
    result_kwargs: dict,
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 + AC2 + AC3 (positive path): a ``NarrationTurnResult`` carrying
    ONLY the named trigger reason (a) reaches dispatch, and (b) emits
    ``render.trigger`` with the matching ``reason`` attribute.

    Wire under test is ``_maybe_dispatch_render`` — the test asserts on
    the watcher event the GM panel actually receives, not on a
    library-internal span. If this passes but the GM panel never sees
    the event (e.g. the span was emitted but the route was missing),
    `test_render_trigger_span_route_registered` catches that.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / f"render_{reason}.png"),
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

    cap = await _bind_capture()
    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    result = NarrationTurnResult(**result_kwargs)

    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is not None, (
        f"reason={reason} fixture did not dispatch — the policy gate rejected an eligible turn"
    )

    # Drain the background daemon round-trip so we don't leak the task.
    await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    triggers = _watcher_events_matching(cap, field="render", op="trigger")
    assert len(triggers) == 1, (
        f"reason={reason}: expected exactly one render.trigger "
        f"watcher event, got {len(triggers)} — events: {cap.events!r}"
    )
    fields = triggers[0]["fields"]
    assert fields.get("reason") == reason, (
        f"render.trigger fired with reason={fields.get('reason')!r}; expected {reason!r}"
    )
    assert fields.get("eligible") is True
    assert fields.get("queued") is True
    assert fields.get("turn_number") == sd.snapshot.turn_manager.interaction


@pytest.mark.asyncio
async def test_render_trigger_resolved_encounter_emits_resolved(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: ``ENCOUNTER_RESOLVED`` is the only trigger reason whose
    signal is NOT carried on ``NarrationTurnResult`` — it's a boolean
    derived in ``narration_apply`` when a confrontation transitions to
    a terminal state. The dispatch seam must accept this signal as an
    out-of-band parameter; threading it through is part of the wiring
    this story must land.

    The test asserts the contract: when the call site signals an
    encounter resolution, ``render.trigger`` fires with reason="resolved"
    and the turn dispatches. If the implementation cannot accept the
    boolean at the call site, the test fails with a ``TypeError`` —
    which is a *correct* RED signal that the wire is missing.
    """
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "render_resolved.png"),
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

    cap = await _bind_capture()
    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    result = NarrationTurnResult(
        narration="The bandit goes down hard.",
        visual_scene=_visual_scene_for_turn(),
        confrontation="bandit_ambush",
    )

    # The wire-first contract: ``encounter_resolved_this_turn`` is
    # threaded into the dispatch seam. The keyword name is part of the
    # wire that Dev must implement — if Dev exposes it under another
    # name, this test fails loudly and that's the right RED signal.
    queued = handler._maybe_dispatch_render(  # noqa: SLF001
        sd, result, encounter_resolved_this_turn=True
    )
    assert queued is not None
    await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    triggers = _watcher_events_matching(cap, field="render", op="trigger")
    assert len(triggers) == 1
    assert triggers[0]["fields"].get("reason") == "resolved"


@pytest.mark.asyncio
async def test_render_trigger_banter_emits_none_policy_skip(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: a banter turn — narrator emitted a ``visual_scene`` but the
    structured signals are empty — must NOT dispatch and must emit
    ``render.trigger`` with reason="none_policy".

    The negative case is the load-bearing one: pre-story the dispatch
    *would* have fired (because ``visual_scene`` is present), and the
    GM panel got no signal. After the wire lands, the GM panel must
    see exactly one ``render.trigger`` event with ``queued=False`` AND
    one ``render.policy_skip`` event."""
    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "should_not_fire.png"),
            "width": 1,
            "height": 1,
            "elapsed_ms": 1,
        }
    )
    await daemon.start(sock)

    monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda: _client_bound_to(sock),
    )

    cap = await _bind_capture()
    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    # Banter turn: visual_scene present, but ZERO structured signals.
    # location matches the snapshot, no beats, no new NPCs, no
    # encounter resolution.
    result = NarrationTurnResult(
        narration="They share a cigarette.",
        visual_scene=_visual_scene_for_turn(),
        location=sd.snapshot.character_locations.get(sd.player_name),
    )

    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is None, (
        "banter turn dispatched — the policy gate failed open. The pre-"
        "story behavior dispatched on visual_scene presence; if this "
        "passes None, the new gate is in place"
    )

    # The daemon should never receive a request. Give the loop a tick to
    # settle so the assertion below isn't racing a pending coroutine.
    await asyncio.sleep(0.1)
    assert daemon.requests == [], (
        f"banter turn produced {len(daemon.requests)} daemon request(s) — policy gate is bypassed"
    )
    await daemon.stop()

    triggers = _watcher_events_matching(cap, field="render", op="trigger")
    assert len(triggers) == 1, (
        f"banter turn must emit exactly one render.trigger "
        f"(reason=none_policy); got {len(triggers)}"
    )
    fields = triggers[0]["fields"]
    assert fields.get("reason") == "none_policy"
    assert fields.get("eligible") is False
    assert fields.get("queued") is False
    assert fields.get("had_visual_scene") is True, (
        "had_visual_scene must reflect that the narrator DID emit a "
        "visual_scene — distinguishes 'narrator didn't try' from "
        "'narrator tried but no policy match'"
    )

    skips = _watcher_events_matching(cap, field="render", op="policy_skip")
    assert len(skips) == 1, f"banter turn must emit one render.policy_skip; got {len(skips)}"
    skip_fields = skips[0]["fields"]
    assert skip_fields.get("reason") == "none_policy"
    assert skip_fields.get("narrator_emitted_subject") is True


@pytest.mark.asyncio
async def test_render_trigger_priority_beat_fire_over_npc_intro(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: a single turn carrying multiple positive signals reports the
    HIGHEST-PRIORITY enum value. Priority order per the story context:
    BEAT_FIRE > SCENE_CHANGE > NPC_INTRO > ENCOUNTER_RESOLVED > NONE_POLICY.

    Test pair (BEAT_FIRE vs NPC_INTRO) is the realistic case — many
    encounter beats also introduce a fresh NPC. Without a deterministic
    priority the GM panel sees noisy reasons and Sebastien can't
    correlate dispatches to mechanical events.
    """
    from sidequest.agents.orchestrator import BeatSelection, NpcMention

    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "priority.png"),
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

    cap = await _bind_capture()
    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    result = NarrationTurnResult(
        narration="The trap springs as a stranger ducks into view.",
        visual_scene=_visual_scene_for_turn(),
        beat_selections=[BeatSelection(actor="Felix", beat_id="trap_sprung")],
        npcs_present=[NpcMention(name="Sallow Dree", is_new=True)],
    )

    queued = handler._maybe_dispatch_render(sd, result)  # noqa: SLF001
    assert queued is not None
    await asyncio.wait_for(queue.get(), timeout=2.0)
    await daemon.stop()

    triggers = _watcher_events_matching(cap, field="render", op="trigger")
    assert len(triggers) == 1
    assert triggers[0]["fields"].get("reason") == "beat_fire", (
        "priority order broken: BEAT_FIRE must outrank NPC_INTRO so the "
        "GM panel reports the mechanical event, not the NPC mention"
    )


@pytest.mark.asyncio
async def test_felix_shape_replay_eight_turn_sequence(
    tmp_path: Path,
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6: replay a synthetic 8-turn Felix-shaped sequence — one of each
    positive trigger reason plus four banter turns. Assert four dispatches
    and four ``none_policy`` spans; assert the watcher stream reflects all
    eight decisions in order. This is the scenario that motivated the
    story; if it doesn't hold end-to-end, AC1-AC4 individually passing
    cannot save us.
    """
    from sidequest.agents.orchestrator import BeatSelection, NpcMention

    sock = short_sock
    daemon = _FakeDaemon(
        reply_payload={
            "image_url": str(tmp_path / "replay.png"),
            "width": 1024,
            "height": 768,
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

    cap = await _bind_capture()
    handler, queue = _make_handler_with_queue()
    sd = _make_session_data()
    base_location = sd.snapshot.character_locations.get(sd.player_name)

    # Disable the ADR-050 image pacing throttle for this test — we are
    # testing the trigger POLICY in isolation, and the throttle's default
    # cooldown would suppress turns 2-7 after turn 0 fires (the test runs
    # in milliseconds, well below the cooldown). The throttle has its own
    # tests under test_render_dispatch_throttle.py.
    class _AlwaysAllow:
        cooldown_seconds = 0

        def should_render(self):
            from sidequest.server.image_pacing import ThrottleDecision

            return ThrottleDecision(
                allowed=True,
                reason="test_disabled",
                cooldown_remaining_seconds=0,
            )

        def record_render(self):
            pass

    sd.image_pacing_throttle = _AlwaysAllow()

    # Order matters: assert watcher events arrive in this order.
    sequence: list[tuple[str, NarrationTurnResult]] = [
        (
            "beat_fire",
            NarrationTurnResult(
                narration="The trap springs.",
                visual_scene=_visual_scene_for_turn(),
                location=base_location,
                beat_selections=[
                    BeatSelection(actor="Felix", beat_id="trap_sprung"),
                ],
            ),
        ),
        (
            "none_policy",
            NarrationTurnResult(
                narration="Felix dusts himself off, muttering.",
                visual_scene=_visual_scene_for_turn(),
                location=base_location,
            ),
        ),
        (
            "scene_change",
            NarrationTurnResult(
                narration="The corridor opens into glasswork.",
                visual_scene=_visual_scene_for_turn(),
                location="The Glass Flats",
            ),
        ),
        (
            "none_policy",
            NarrationTurnResult(
                narration="He picks his teeth with a thorn.",
                visual_scene=_visual_scene_for_turn(),
                location="The Glass Flats",
            ),
        ),
        (
            "npc_intro",
            NarrationTurnResult(
                narration="A figure unfolds from the rubble.",
                visual_scene=_visual_scene_for_turn(),
                location="The Glass Flats",
                npcs_present=[NpcMention(name="Sallow Dree", is_new=True)],
            ),
        ),
        (
            "none_policy",
            NarrationTurnResult(
                narration="They trade names. Pleasantries.",
                visual_scene=_visual_scene_for_turn(),
                location="The Glass Flats",
                npcs_present=[
                    NpcMention(name="Sallow Dree", is_new=False),
                ],
            ),
        ),
        (
            "resolved",
            NarrationTurnResult(
                narration="Sallow Dree dies under the lamp.",
                visual_scene=_visual_scene_for_turn(),
                location="The Glass Flats",
                confrontation="ambush_in_glassflats",
            ),
        ),
        (
            "none_policy",
            NarrationTurnResult(
                narration="Felix sits with the body.",
                visual_scene=_visual_scene_for_turn(),
                location="The Glass Flats",
            ),
        ),
    ]

    # Each dispatch advances the snapshot's location to mirror the
    # narration_apply effect — the next turn's scene_change classifier
    # compares against the post-apply location.
    dispatched_count = 0
    for idx, (expected_reason, result) in enumerate(sequence):
        encounter_resolved = expected_reason == "resolved"
        queued = handler._maybe_dispatch_render(  # noqa: SLF001
            sd, result, encounter_resolved_this_turn=encounter_resolved
        )
        if expected_reason == "none_policy":
            assert queued is None, (
                f"turn {idx} ({expected_reason}) dispatched — should have been suppressed"
            )
        else:
            assert queued is not None, f"turn {idx} ({expected_reason}) failed to dispatch"
            dispatched_count += 1
            await asyncio.wait_for(queue.get(), timeout=2.0)
        # Advance the snapshot's location for the next turn so the
        # classifier compares against the right baseline.
        if result.location:
            sd.snapshot.character_locations[sd.player_name] = result.location

    # Give the event loop a tick so async watcher publishes from the final
    # turn (which had no `await queue.get()` to drain) reach the capture.
    await asyncio.sleep(0.1)
    await daemon.stop()

    triggers = _watcher_events_matching(cap, field="render", op="trigger")
    assert len(triggers) == 8, (
        f"expected 8 render.trigger events (one per turn), got {len(triggers)}"
    )
    actual_reasons = [t["fields"].get("reason") for t in triggers]
    expected_reasons = [r for r, _ in sequence]
    assert actual_reasons == expected_reasons, (
        f"watcher event ordering / reasons do not match the turn "
        f"sequence.\n  expected: {expected_reasons}\n  actual:   "
        f"{actual_reasons}"
    )
    assert dispatched_count == 4
    none_policy_triggers = [t for t in triggers if t["fields"].get("reason") == "none_policy"]
    assert len(none_policy_triggers) == 4
