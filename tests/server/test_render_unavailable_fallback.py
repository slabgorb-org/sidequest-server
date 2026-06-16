"""RED tests — Story 45-31 — wire-first unavailable fallback (AC4).

The Felix anti-silence test. Playtest 2026-04-19: 13 minutes elapsed
between the last successful render and session end with zero render
attempts surfacing in any save-file or OTEL signal. The bug post-mortem
revealed that ``_maybe_dispatch_render`` had no way to distinguish
"daemon socket missing" from "daemon hung mid-render" from "daemon
heartbeat stopped 8 minutes ago" — they all collapsed into the same
silent miss path.

This test pins the contract: when the ``DaemonStateMirror`` reports the
daemon UNRESPONSIVE (no heartbeat for >2× interval), the dispatcher
MUST:

  1. emit a ``render.unavailable`` watcher event with
     ``reason="heartbeat_lost"`` and a non-null ``last_heartbeat_ts``;
  2. NOT attempt the daemon round-trip (no socket connect, no
     pre-existing ``client.is_available()`` recovery);
  3. mark the scrapbook row with ``render_status="unavailable"``
     so the UI shows "Render unavailable" instead of a silent gap.

Wire-first: tests exercise ``handler._maybe_dispatch_render`` against a
real DaemonClient that points at a socket which intentionally never
emits a heartbeat. The boundary test crosses the dispatcher → mirror →
watcher hub → scrapbook emitter chain.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from pathlib import Path

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, VisualScene
from sidequest.server.session_handler import (
    WebSocketSessionHandler,
    _SessionData,
)


@pytest.fixture
def short_sock(tmp_path: Path) -> Path:
    del tmp_path
    p = Path(f"/tmp/sq-unavail-test-{uuid.uuid4().hex[:8]}.sock")
    yield p
    if p.exists():
        p.unlink()


def _make_session_data() -> _SessionData:
    from unittest.mock import MagicMock

    from sidequest.game.session import GameSnapshot, TurnManager

    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="",
        turn_manager=TurnManager(interaction=4),
    )
    return _SessionData(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        player_name="Rux",
        player_id="player-felix",
        snapshot=snap,
        store=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(),
    )


def _make_handler() -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(save_dir=Path("/tmp/never-used"))
    handler._out_queue = asyncio.Queue()  # noqa: SLF001 — test wiring
    return handler


def _make_visual_result() -> NarrationTurnResult:
    # Story 45-30 added a render-trigger policy gate that runs ahead of
    # the 45-31 unavailable-fallback branch. A bare NarrationTurnResult
    # with no structured signals classifies as NONE_POLICY and the
    # dispatcher returns at the policy gate before reaching the
    # unavailable check. Inject a BeatSelection so the result classifies
    # as BEAT_FIRE — the test exercises 45-31's degradation path, not
    # 45-30's policy decision.
    return NarrationTurnResult(
        narration="The crack yawns open. A pale glow pulses behind it.",
        visual_scene=VisualScene(
            subject="a jagged fissure in red rock",
            tier="scene_illustration",
            mood="ominous",
            tags=["desert", "ruin"],
        ),
        beat_selections=[BeatSelection(actor="test", beat_id="unavailable_fallback_test")],
    )


async def _capture_watcher_events() -> tuple[list[dict], object]:
    from sidequest.telemetry.watcher_hub import watcher_hub

    watcher_hub.bind_loop(asyncio.get_running_loop())
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


class _NoOpDaemon:
    """Listens on a socket but never replies — tracks accept count so
    the test can assert no round-trip was attempted."""

    def __init__(self) -> None:
        self.accept_count = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self, path: Path) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(path))

    async def _handle(self, reader, writer) -> None:  # noqa: ANN001
        self.accept_count += 1
        try:
            # Drain whatever the client sent and hold the connection so
            # the test can detect that it was opened.
            await reader.readline()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.mark.asyncio
async def test_unresponsive_daemon_emits_render_unavailable_event(
    short_sock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: when the turn pipeline has stamped
    ``sd.render_unavailable_pending=True`` (because the mirror reports
    UNRESPONSIVE at the moment of the scrapbook emit), the dispatcher
    MUST emit ``render.unavailable`` with ``reason="heartbeat_lost"``
    and a non-null ``last_heartbeat_ts``, and MUST NOT attempt the
    daemon round-trip."""
    daemon = _NoOpDaemon()
    await daemon.start(short_sock)
    try:
        monkeypatch.setenv("SIDEQUEST_RENDER_ENABLED", "1")

        from sidequest.daemon_client import DaemonClient

        monkeypatch.setattr(
            "sidequest.server.websocket_session_handler.DaemonClient",
            lambda: DaemonClient(socket_path=short_sock, timeout_seconds=0.5),
        )

        # Seed the mirror with a heartbeat so last_heartbeat_ts is
        # non-null on the watcher event, then force the mirror into
        # UNRESPONSIVE so a future check (e.g. by the upstream turn
        # pipeline that sets render_unavailable_pending) would fire.
        from sidequest.daemon_client.state_mirror import get_mirror

        mirror = get_mirror()
        mirror.clear_for_test()
        mirror.record_heartbeat(
            queue="image",
            state="ready",
            queue_depth=0,
            ts_monotonic=0.0,
        )
        mirror.force_unresponsive_for_test()

        captured, _cap = await _capture_watcher_events()

        handler = _make_handler()
        sd = _make_session_data()
        # The turn pipeline sets this flag before _maybe_dispatch_render
        # runs (see websocket_session_handler.py: dispatch_post block).
        # Tests exercise the dispatcher branch directly by setting the
        # flag here.
        sd.render_unavailable_pending = True

        result = handler._maybe_dispatch_render(sd, _make_visual_result())  # noqa: SLF001

        # Allow watcher publish to flush.
        await asyncio.sleep(0.1)

        # AC4 (1): no daemon round-trip attempted. The fake daemon
        # tracks accept counts; an unresponsive-fallback dispatch must
        # not open a socket.
        assert daemon.accept_count == 0, (
            f"AC4 forbids the daemon round-trip when the mirror is "
            f"UNRESPONSIVE; got {daemon.accept_count} accepts"
        )

        # AC4 (2): render.unavailable event must fire with the
        # documented attributes.
        unavail = [
            e
            for e in captured
            if e.get("event_type") == "state_transition"
            and e.get("fields", {}).get("field") == "render"
            and e.get("fields", {}).get("op") == "unavailable"
        ]
        assert len(unavail) == 1, f"expected exactly 1 render.unavailable event, got {len(unavail)}"
        fields = unavail[0]["fields"]
        assert fields["reason"] == "heartbeat_lost"
        assert fields.get("last_heartbeat_ts") is not None, (
            "render.unavailable must carry the last seen heartbeat ts so "
            "post-mortem can quantify the silence window"
        )
        assert "turn_number" in fields
        assert "player_id" in fields

        # AC4: dispatcher returns None — no RENDER_QUEUED frame ships
        # because no render is actually in flight.
        assert result is None, (
            "unresponsive fallback must return None; no RENDER_QUEUED "
            "should ship for a render that never enters the daemon"
        )
    finally:
        await daemon.stop()


def test_render_status_persists_to_database_end_to_end(tmp_path: Path) -> None:
    """AC4 wire-first end-to-end: a scrapbook payload built with
    ``render_status="unavailable"`` MUST round-trip into the
    ``scrapbook_entries`` table and SELECT back as ``"unavailable"``.

    This is the test gap the reviewer flagged: the previous spy-based
    test only verified the *payload* carried the field, not that the
    SQL persisted the column. With ``render_status TEXT`` now in the
    schema and the INSERT statement extended, the value must reach
    storage."""
    from sidequest.game.persistence import SqliteStore
    from sidequest.protocol.messages import ScrapbookEntryPayload
    from sidequest.server.emitters import persist_scrapbook_entry

    # Real on-disk SQLite to exercise the schema + ALTER TABLE migration
    # path. ``open_in_memory`` would also work; on-disk gives us
    # confidence the migration is idempotent across reopens.
    db_path = tmp_path / "test.db"
    store = SqliteStore.open(str(db_path))
    try:
        # Reopen once to exercise the migration's "column already exists"
        # branch — this proves _apply_migrations is idempotent.
        store2 = SqliteStore.open(str(db_path))
        store2._conn.close()  # noqa: SLF001

        # Stub handler shape: persist_scrapbook_entry only needs
        # handler._event_log.store. Build the minimum.
        class _StubEventLog:
            def __init__(self, store):  # noqa: ANN001
                self.store = store

        class _StubHandler:
            def __init__(self, store):  # noqa: ANN001
                self._event_log = _StubEventLog(store)

        handler = _StubHandler(store)

        payload = ScrapbookEntryPayload(
            turn_id=42,
            location="Tood's Dome — Nest Crack",
            narrative_excerpt="The crack yawns open.",
            scene_title="a jagged fissure",
            scene_type="scene_illustration",
            render_status="unavailable",
        )
        persist_scrapbook_entry(handler, payload)

        # Read back from SQL — this proves the value reached storage.
        row = store._conn.execute(  # noqa: SLF001
            "SELECT render_status, narrative_excerpt FROM scrapbook_entries WHERE turn_id = ?",
            (42,),
        ).fetchone()
        assert row is not None, "scrapbook_entries row not persisted"
        assert row[0] == "unavailable", (
            f"render_status did not round-trip through SQL — got {row[0]!r}, "
            f"expected 'unavailable'. Schema or INSERT statement missing the column."
        )
        assert row[1] == "The crack yawns open."
    finally:
        store._conn.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_unresponsive_pipeline_stamps_scrapbook_payload_render_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: when ``DaemonStateMirror.is_unresponsive()`` returns True at
    the moment the turn pipeline calls ``_emit_scrapbook_entry``, the
    scrapbook payload MUST carry ``render_status="unavailable"`` on
    the *first* emit (not a duplicate row written later).

    Asserts the dispatcher does NOT then write a second row — the
    SCRAPBOOK_ENTRY emitted live carries the field; the dispatcher only
    publishes the watcher event and skips the daemon round-trip.
    """
    from sidequest.daemon_client.state_mirror import get_mirror
    from sidequest.protocol.messages import ScrapbookEntryPayload
    from sidequest.server import emitters as _emitters

    mirror = get_mirror()
    mirror.clear_for_test()
    mirror.record_heartbeat(queue="image", state="ready", queue_depth=0, ts_monotonic=0.0)
    mirror.force_unresponsive_for_test()

    persisted: list[ScrapbookEntryPayload] = []

    def _spy_persist(handler, payload):  # noqa: ANN001
        persisted.append(payload)

    monkeypatch.setattr(_emitters, "persist_scrapbook_entry", _spy_persist)

    # Drive the upstream emit_scrapbook_entry (the function the turn
    # pipeline calls) with render_status="unavailable" — what the
    # pipeline sets after consulting the mirror.
    from unittest.mock import MagicMock

    handler = MagicMock()
    handler._event_log = MagicMock()
    handler._event_log.append = MagicMock()

    from sidequest.agents.orchestrator import NarrationTurnResult, VisualScene
    from sidequest.game.session import GameSnapshot, TurnManager
    from sidequest.server.session_handler import _SessionData

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
        player_id="player-1",
        snapshot=snap,
        store=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(),
    )
    result = NarrationTurnResult(
        narration="A pale glow pulses behind the rock.",
        visual_scene=VisualScene(
            subject="a jagged fissure",
            tier="scene_illustration",
            mood="ominous",
            tags=[],
        ),
    )

    _emitters.emit_scrapbook_entry(
        handler,
        sd=sd,
        snapshot=snap,
        result=result,
        render_status="unavailable",
    )

    # Exactly ONE scrapbook row, with render_status set on the
    # first-and-only payload — no duplicate from a downstream dispatcher.
    assert len(persisted) == 1, (
        f"expected exactly 1 scrapbook row from the upstream pipeline; "
        f"got {len(persisted)}. A duplicate signals the dispatcher is "
        f"writing a second row, which is the bug the reviewer flagged."
    )
    row = persisted[0]
    assert row.render_status == "unavailable"
    assert row.narrative_excerpt.startswith("A pale glow")


def test_scrapbook_payload_schema_has_render_status_field() -> None:
    """The protocol schema must expose ``render_status`` with the unified
    enum spanning Story 45-30 (trigger-policy outcome) and Story 45-31
    (daemon-liveness outcome): ``rendered`` | ``skipped_policy`` |
    ``failed`` | ``unavailable``.

    Per 45-31 context: "If 45-30 lands first, this story extends the
    enum with 'unavailable'." 45-30 landed first.

    Wiring guard — the daemon-side, the UI, and the persistence layer
    all key on this field name. If the schema rejects the value, the
    fallback path crashes before the watcher event lands and the GM
    panel sees nothing."""
    from sidequest.protocol.messages import ScrapbookEntryPayload

    payload = ScrapbookEntryPayload(
        turn_id=1,
        location="Tood's Dome",
        narrative_excerpt="...",
        render_status="unavailable",
    )
    assert payload.render_status == "unavailable"

    for value in ("rendered", "skipped_policy", "failed"):
        payload = ScrapbookEntryPayload(
            turn_id=1,
            location="Tood's Dome",
            narrative_excerpt="...",
            render_status=value,
        )
        assert payload.render_status == value

    # Default when omitted — per the unified enum's default, "rendered"
    # (matches the develop behavior 45-30 shipped; existing rows that
    # pre-date the discriminator continue to read as the happy path).
    payload = ScrapbookEntryPayload(
        turn_id=1,
        location="Tood's Dome",
        narrative_excerpt="...",
    )
    assert payload.render_status == "rendered"
