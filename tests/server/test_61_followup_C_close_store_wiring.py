"""Story 61-followup-C — close_store() wiring on last disconnect.

Wiring test. The 61-4 baseline-reset machinery (``reset_baselines`` on
``AnthropicSdkClient``) is invoked from ``SessionRoom.close_store()``,
but ``close_store`` has no production callers until this story; the
``RoomRegistry`` never evicts rooms, so without a teardown trigger the
orchestrator's SDK client persists across multiple sessions on the same
slug with an unreset baseline — the cross-session-state hazard that
61-followup-A partly addressed by session-id-keying the baselines.

This story wires ``close_store()`` into the last-disconnect path inside
``ws_endpoint``. When a real player disconnect leaves the room with zero
connected players, ``close_store()`` must fire. Transient HMR
disconnects (multi-socket player, one socket closes) must NOT fire it.

Test strategy: drive a real ``ws_endpoint`` end-to-end via a minimal
fake handler / fake WebSocket pair. This is a fixture-driven behaviour
test per ``sidequest-server/CLAUDE.md`` "Every Test Suite Needs a Wiring
Test" — no source-text grep, no ``handler_path.read_text()``: we call
the real production endpoint and assert on the room's mock store.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState

from sidequest.game.persistence import GameMode
from sidequest.server.session_room import RoomRegistry, SessionRoom
from sidequest.server.websocket import ws_endpoint


def _fake_ws() -> SimpleNamespace:
    """A WebSocket that accepts, then disconnects cleanly on first read.

    Triggers the ``WebSocketDisconnect`` branch in ``ws_endpoint`` so
    control reaches the ``finally`` block where the close_store wiring
    lives.
    """

    ws = SimpleNamespace(
        client=("127.0.0.1", 54321),
        app=SimpleNamespace(state=SimpleNamespace(room_registry=RoomRegistry())),
        application_state=WebSocketState.CONNECTED,
        client_state=WebSocketState.CONNECTED,
        headers={"host": "localhost"},
    )

    async def accept() -> None:
        return None

    async def receive_text() -> str:
        raise WebSocketDisconnect(code=1000)

    async def close(code: int = 1000) -> None:  # noqa: ARG001
        ws.application_state = WebSocketState.DISCONNECTED

    async def send_text(_text: str) -> None:
        return None

    ws.accept = accept
    ws.receive_text = receive_text
    ws.close = close
    ws.send_text = send_text
    return ws


class _PinnedRoomHandler:
    """Minimal WebSocketSessionHandler that pins a pre-built room.

    ``ws_endpoint`` calls ``attach_room_context`` right after ``accept()``
    with the socket_id it generated. We use that hook to register the
    socket on our pre-built room, so the ``finally``-block
    ``room.disconnect(socket_id=...)`` actually finds and removes the
    player. ``current_room()`` then returns the same room for cleanup.

    ``cleanup_behavior`` controls what the awaited cleanup() does:

    - ``"normal"`` (default): call ``room.save()``, increment counter, return.
      Mirrors the production happy path through
      ``WebSocketSessionHandler.cleanup()`` → ``room.save()``.
    - ``"raise"``: raise ``RuntimeError`` mid-cleanup before save runs.
      Models a programmer bug or unexpected exception bubbling out of
      ``cleanup()`` itself — the finally-block must still tear down the
      room and emit a loud breadcrumb (CLAUDE.md: No Silent Fallbacks).
    - ``"swallow_save_exception"``: increment counter but do NOT save,
      return normally. Models the production behaviour where
      ``WebSocketSessionHandler.cleanup()`` catches a save Exception at
      ``websocket_session_handler.py:1551-1557``, logs ``session.disconnect_save_failed``,
      sets ``self.last_save_failure = exc`` (added by Story 61-followup-C
      so ws_endpoint can see the swallowed failure), and returns without
      re-raising. In that case ``close_store()`` must NOT fire —
      otherwise the canonical store is torn down with the final
      snapshot lost.
    """

    def __init__(
        self,
        room: SessionRoom,
        *,
        prewire: list[tuple[str, str]] | None = None,
        endpoint_player: str | None = None,
        cleanup_behavior: str = "normal",
    ) -> None:
        # prewire: pairs of (player_id, socket_id) to connect BEFORE
        # ws_endpoint runs (e.g. HMR-survivor socket, MP peer).
        # endpoint_player: the player_id whose socket attach_room_context
        # registers with the endpoint's own generated socket_id. If None,
        # attach_room_context only captures the socket_id and never
        # connects the endpoint socket to a player — useful for pre-bind
        # disconnect scenarios.
        self._room = room
        self._endpoint_player = endpoint_player
        self._cleanup_behavior = cleanup_behavior
        self.cleanup_calls = 0
        self.last_save_failure: Exception | None = None
        for player_id, socket_id in prewire or ():
            room.connect(player_id, socket_id=socket_id)

    async def cleanup(self) -> None:
        """Mirror selected ``WebSocketSessionHandler.cleanup()`` behaviours.

        The fixture is deliberately simpler than the production handler:
        it does not gate on ``_session_data`` or ``_room`` preconditions,
        and the ``"normal"`` mode unconditionally calls ``room.save()``.
        That is sufficient to exercise the save→close ordering contract
        (and would catch the round-1 spec-check regression where
        close_store was placed before cleanup), but it does NOT reproduce
        the production cleanup's full conditional save shape. The other
        two behaviours model the production failure modes that the
        unconditional-save mock would otherwise hide.
        """
        self.cleanup_calls += 1
        if self._cleanup_behavior == "normal":
            self._room.save()
        elif self._cleanup_behavior == "raise":
            raise RuntimeError("simulated cleanup failure mid-save")
        elif self._cleanup_behavior == "swallow_save_exception":
            # Simulate WebSocketSessionHandler.cleanup()'s
            # try/except around room.save(): the save raised, the
            # handler logged at error, and the exception was swallowed.
            # No save happens; cleanup returns normally; the final
            # snapshot is lost.
            self.last_save_failure = RuntimeError("simulated save failure")
        else:  # pragma: no cover — guard against typos in test params
            raise ValueError(f"unknown cleanup_behavior: {self._cleanup_behavior!r}")

    def attach_room_context(
        self,
        *,
        registry: RoomRegistry | None = None,
        socket_id: str,
        out_queue: Any,
        player_identity: str | None = None,
        player_identity_source: str | None = None,
    ) -> None:
        del registry, player_identity, player_identity_source  # not used by this test
        if self._endpoint_player is not None:
            self._room.connect(self._endpoint_player, socket_id=socket_id)
            self._room.attach_outbound(socket_id, out_queue)

    def current_room(self) -> SessionRoom | None:
        return self._room

    async def handle_message(self, _msg: Any) -> list[Any]:
        # Never reached; receive_text raises before any message arrives.
        return []


def _mock_room_with_baseline_tracking(
    room: SessionRoom,
) -> MagicMock:
    """Wire a MagicMock orchestrator onto ``room`` so close_store()'s
    reset_baselines() call site can be asserted.

    SessionRoom.close_store() reaches into ``self._orchestrator._client.reset_baselines``
    and invokes it with ``self.slug``. The 61-4/61-followup-A motivation
    for this entire story is that the cost-rolling-baseline must be reset
    on teardown — without this assertion, a refactor that removes the
    reset_baselines call would pass the suite (the wiring test only
    checks store.close, not the actual baseline reset).

    Returns the mock orchestrator so the test can assert on
    ``mock._client.reset_baselines.call_count`` and ``.call_args``.
    """
    mock_orch = MagicMock()
    mock_orch._client = MagicMock()
    mock_orch._client.reset_baselines = MagicMock()
    # close_store reads ``self._orchestrator`` directly (session_room.py:353).
    # Bypass the get_or_create factory by writing the underscore field —
    # this is internal access reserved for tests of close_store's reset
    # contract; production code goes through get_or_create_orchestrator.
    room._orchestrator = mock_orch
    return mock_orch


@pytest.mark.asyncio
async def test_ws_endpoint_calls_close_store_when_last_player_disconnects():
    """Wiring: solo room, single socket disconnects → room empty → close_store fires.

    This is the load-bearing assertion for the story: the production
    endpoint (``ws_endpoint``) is the only call site for
    ``room.close_store()`` after this change. Without the wiring, the
    rolling baseline survives across session reuse and the cost runaway
    alarm (61-4) silences itself over time.
    """
    room = SessionRoom(slug="slug-wire-solo", mode=GameMode.SOLO)
    store = MagicMock()
    room.bind_world(snapshot=MagicMock(), store=store)
    mock_orch = _mock_room_with_baseline_tracking(room)

    handler = _PinnedRoomHandler(room, endpoint_player="alice")

    ws = _fake_ws()
    await ws_endpoint(ws, handler)

    assert room.connected_player_ids() == [], (
        "After the only player's socket disconnects, the room should be empty"
    )
    assert store.close.call_count == 1, (
        "Wiring failure: ws_endpoint did not call room.close_store() "
        "when the last player disconnected. Expected exactly one close() "
        f"on the mock store, got {store.close.call_count}."
    )
    assert handler.cleanup_calls == 1, "handler.cleanup() must run exactly once"

    # Ordering guard (Architect spec-check finding 2026-05-24): the final
    # snapshot save in handler.cleanup() must run BEFORE close_store() nulls
    # _store, otherwise the final save silently no-ops on the now-None store
    # (session_room.py:277). All save and close calls flow through the same
    # MagicMock store, so its method_calls list preserves order.
    method_names = [c[0] for c in store.method_calls]
    assert "save" in method_names, (
        "handler.cleanup() must call room.save() (which calls store.save) "
        f"before teardown. Got method_calls={store.method_calls!r}"
    )
    assert "close" in method_names, "store.close must be called by close_store()"
    assert method_names.index("save") < method_names.index("close"), (
        "Ordering failure: store.close was called before store.save. "
        "close_store() must run AFTER handler.cleanup() persists the final "
        "snapshot — otherwise the final save silently no-ops. "
        f"Observed order: {method_names}"
    )

    # Load-bearing motivation assertion (Reviewer test-analyzer 2026-05-24
    # HIGH finding): the entire point of close_store() firing is that it
    # triggers `reset_baselines()` on the orchestrator's SDK client so the
    # rolling cost-baseline does not carry across session-reuse on the
    # same slug. The store.close mock alone does not prove this — a
    # refactor that drops the reset_baselines call would still satisfy
    # the close.call_count assertion above. Assert the baseline-reset
    # explicitly here.
    assert mock_orch._client.reset_baselines.call_count == 1, (
        "Wiring failure: close_store() did not call reset_baselines() on "
        "the orchestrator's SDK client. This is the cost-control motivation "
        "for the entire story (61-4 + 61-followup-A). Got "
        f"call_count={mock_orch._client.reset_baselines.call_count}."
    )
    assert mock_orch._client.reset_baselines.call_args == ((room.slug,), {}), (
        "reset_baselines must be called with room.slug as the session_id "
        "(per session_helpers.py session_id=sd.game_slug). Got "
        f"call_args={mock_orch._client.reset_baselines.call_args!r}."
    )


@pytest.mark.asyncio
async def test_ws_endpoint_does_not_close_store_on_transient_hmr_disconnect():
    """Wiring: multi-socket player, one socket closes → room still has
    that player on the surviving socket → close_store must NOT fire.

    Mirrors the Vite/HMR tab-reload pattern documented in
    ``session_room.connect()``: a single ``player_id`` can hold multiple
    sockets at once, and only the *last* socket's departure should
    teardown the room. Story 45-7 (pingpong 2026-05-07) added the
    multi-socket refcounting; this test guards the 61-followup-C
    teardown trigger against false positives on that path.
    """
    room = SessionRoom(slug="slug-wire-hmr", mode=GameMode.SOLO)
    store = MagicMock()
    room.bind_world(snapshot=MagicMock(), store=store)

    # Pre-wire one of alice's sockets (the HMR-survivor). The endpoint's
    # own socket becomes the second socket and is the one that
    # disconnects when receive_text() raises.
    handler = _PinnedRoomHandler(
        room,
        prewire=[("alice", "sock-survivor")],
        endpoint_player="alice",
    )

    ws = _fake_ws()
    await ws_endpoint(ws, handler)

    assert room.connected_player_ids() == ["alice"], (
        "Alice should still be connected on the surviving socket"
    )
    assert store.close.call_count == 0, (
        "Wiring failure: close_store() fired on a transient (multi-socket) "
        "disconnect. It must only fire when the room is fully empty."
    )
    assert handler.cleanup_calls == 1, "handler.cleanup() must run exactly once"


@pytest.mark.asyncio
async def test_ws_endpoint_does_not_close_store_on_mid_mp_disconnect():
    """Wiring: multiplayer room with two players, one leaves → room
    still has the other player → close_store must NOT fire.

    Solo-only teardown is not the contract: a multiplayer mid-game
    disconnect must not nuke the canonical store and reset baselines
    while another player is still playing. Only the *last* player's
    departure triggers teardown.
    """
    room = SessionRoom(slug="slug-wire-mp-mid", mode=GameMode.MULTIPLAYER)
    store = MagicMock()
    room.bind_world(snapshot=MagicMock(), store=store)
    mock_orch = _mock_room_with_baseline_tracking(room)

    # Pre-wire bob on his own socket. Alice's socket is the endpoint's,
    # bound via the endpoint_player kwarg.
    handler = _PinnedRoomHandler(
        room,
        prewire=[("bob", "sock-bob")],
        endpoint_player="alice",
    )

    ws = _fake_ws()
    await ws_endpoint(ws, handler)

    assert room.connected_player_ids() == ["bob"], (
        "Bob should still be connected after Alice leaves"
    )
    assert store.close.call_count == 0, (
        "Wiring failure: close_store() fired while another player was "
        "still connected. MP mid-game teardown must be inhibited."
    )
    assert handler.cleanup_calls == 1, (
        "handler.cleanup() must run exactly once in the MP mid-game "
        "disconnect path. Without this guard, a regression that skipped "
        "cleanup() entirely on MP would still satisfy "
        "store.close.call_count == 0 vacuously (no save fired, no close fired)."
    )
    assert mock_orch._client.reset_baselines.call_count == 0, (
        "reset_baselines must not fire while another player is still "
        "connected — the cost-baseline belongs to the active session, "
        "not the departed player."
    )


@pytest.mark.asyncio
async def test_ws_endpoint_calls_close_store_when_last_mp_player_disconnects():
    """Wiring: multiplayer room with one seated player → last player
    disconnects → room is empty → close_store fires.

    The story's SM Acceptance Bar bullet
    `MP game, last player disconnects: room is now empty → close_store()
    fires ✓` was previously unasserted. The teardown gate at
    ``websocket.py:202`` is mode-agnostic
    (``not room.connected_player_ids()``), but missing a multiplayer
    last-player test means a mode-specific regression (e.g. accidentally
    gating teardown on ``GameMode.SOLO``) would slip through. This test
    mirrors the solo wiring test under MULTIPLAYER mode and asserts the
    full contract: close fires once, save precedes close, reset_baselines
    fires with the room slug.
    """
    room = SessionRoom(slug="slug-wire-mp-last", mode=GameMode.MULTIPLAYER)
    store = MagicMock()
    room.bind_world(snapshot=MagicMock(), store=store)
    mock_orch = _mock_room_with_baseline_tracking(room)

    handler = _PinnedRoomHandler(room, endpoint_player="alice")

    ws = _fake_ws()
    await ws_endpoint(ws, handler)

    assert room.connected_player_ids() == [], (
        "After the last MP player disconnects, the room should be empty"
    )
    assert store.close.call_count == 1, (
        "Wiring failure: close_store() did not fire on the last MP "
        f"player's disconnect. Got close.call_count={store.close.call_count}."
    )
    assert handler.cleanup_calls == 1, "handler.cleanup() must run exactly once"

    method_names = [c[0] for c in store.method_calls]
    assert "save" in method_names and "close" in method_names, (
        "Both save and close must run on the last MP player's disconnect. "
        f"Got method_calls={store.method_calls!r}"
    )
    assert method_names.index("save") < method_names.index("close"), (
        "Ordering failure in MP last-player path: store.close was called "
        f"before store.save. Observed order: {method_names}"
    )

    assert mock_orch._client.reset_baselines.call_count == 1, (
        "MP last-player wiring failure: reset_baselines() did not fire "
        f"on the cost-control side. Got call_count={mock_orch._client.reset_baselines.call_count}."
    )
    assert mock_orch._client.reset_baselines.call_args == ((room.slug,), {}), (
        "MP last-player reset_baselines must be called with room.slug. "
        f"Got call_args={mock_orch._client.reset_baselines.call_args!r}."
    )


# ---------------------------------------------------------------------------
# Cleanup exception-safety contract — regression guards
# (originated as Reviewer 2026-05-24 HIGH finding 1, RED→GREEN landed in
#  RT1 commits 88cc9ee + 6b6e9e1 + 61abe22; further hardened in RT2 with
#  the explicit asyncio.CancelledError re-raise per Reviewer RT1 HIGH 1)
#
# close_store's teardown gate has two failure modes that the round-1
# delivery did not initially defend. Both are now production-protected
# and these tests are the regression guards.
#
# A. handler.cleanup() itself raises (uncaught Exception bubbles out)
#    → ws_endpoint catches at websocket.py:171, logs ws.cleanup_failed at
#    ERROR with the slug, sets cleanup_failed=True, and the teardown gate
#    at websocket.py:202 skips close_store with a ws.room_teardown_skipped
#    reason=cleanup_raised breadcrumb. asyncio.CancelledError is re-raised
#    explicitly (websocket.py:160) so shutdown propagates correctly.
#
# B. handler.cleanup() catches a save-side exception internally and returns
#    normally (production WebSocketSessionHandler.cleanup() at
#    websocket_session_handler.py:1551-1557 — it logs session.disconnect_save_failed
#    AND sets self.last_save_failure = exc, added in this story specifically
#    so ws_endpoint can detect the swallow). ws_endpoint reads
#    handler.last_save_failure (websocket.py:201) and the teardown gate
#    skips close_store with reason=save_failure_swallowed. Without this,
#    the canonical store would be torn down with the final snapshot lost.
#
# The two tests below GREEN against the current production code. They
# exist as regression guards: if a future refactor removes the
# cleanup_failed gate, the last_save_failure read, or the explicit
# CancelledError re-raise, these tests will fail.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_endpoint_logs_and_skips_close_store_when_cleanup_raises(caplog):
    """Regression guard: when handler.cleanup() raises an uncaught
    Exception, the finally block must (a) not crash the WebSocket
    teardown, (b) emit a loud ERROR breadcrumb with the slug
    (``ws.cleanup_failed``), and (c) skip close_store — with a SECOND
    breadcrumb (``ws.room_teardown_skipped reason=cleanup_raised``) so
    the skipped teardown is visible in operator tails.

    Production wiring: ws_endpoint at websocket.py:171 catches Exception
    (re-raising asyncio.CancelledError explicitly at :160 so shutdown
    cancellation propagates correctly). The cleanup_failed flag then
    drives the teardown gate at websocket.py:202 to skip close_store
    and emit the ``ws.room_teardown_skipped`` log instead. This test
    asserts both halves (the log presence and the skip behaviour) so a
    regression that removed either guard would fail loudly.
    """
    import logging

    room = SessionRoom(slug="slug-wire-cleanup-raises", mode=GameMode.SOLO)
    store = MagicMock()
    room.bind_world(snapshot=MagicMock(), store=store)
    mock_orch = _mock_room_with_baseline_tracking(room)

    handler = _PinnedRoomHandler(
        room,
        endpoint_player="alice",
        cleanup_behavior="raise",
    )

    ws = _fake_ws()
    with caplog.at_level(logging.ERROR, logger="sidequest.server.websocket"):
        # The RuntimeError from cleanup must not crash ws_endpoint —
        # it should be caught and logged. If this raises, the production
        # code re-throws cleanup exceptions and the WebSocket layer above
        # sees an exception it cannot diagnose.
        await ws_endpoint(ws, handler)

    assert handler.cleanup_calls == 1, "cleanup() must have been invoked"

    # Loud breadcrumb at the skip site (No Silent Fallbacks). The exact
    # log key is policy — Dev may pick `ws.cleanup_failed` or similar —
    # but it MUST include the slug so operator tails can correlate.
    error_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "slug-wire-cleanup-raises" in r.getMessage()
    ]
    assert error_records, (
        "ws_endpoint must emit an ERROR-level log line referencing the "
        "slug when handler.cleanup() raises. Got no such record. "
        f"Captured: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )

    # Reviewer 2026-05-24 RT2 HIGH 2: the test name and docstring claim TWO
    # breadcrumbs fire (ws.cleanup_failed AND ws.room_teardown_skipped). The
    # `error_records` filter above is satisfied by either alone — both contain
    # the slug at ERROR level. Assert the second breadcrumb independently so
    # a regression that removed the else-branch log at websocket.py:210-215
    # (the No-Silent-Fallbacks teardown-skip breadcrumb this story explicitly
    # delivers) cannot silently regress without failing the suite.
    skipped_records = [
        r
        for r in caplog.records
        if "ws.room_teardown_skipped" in r.getMessage() and "cleanup_raised" in r.getMessage()
    ]
    assert skipped_records, (
        "ws.room_teardown_skipped reason=cleanup_raised breadcrumb must fire "
        "when the teardown gate skips close_store after a cleanup exception. "
        f"Got no such record. Captured: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )

    # Reviewer 2026-05-24 HIGH 3 (round-trip 1 review): the test name says
    # "logs AND skips close_store" but only the log half was asserted.
    # Without the close-count assertion, a regression that removed the
    # `cleanup_failed` gate from websocket.py (letting close_store fire
    # even after a cleanup exception) would still pass — the ERROR log
    # from the try/except is independent of the gate decision.
    assert store.close.call_count == 0, (
        "Wiring failure: close_store() must NOT fire when cleanup raised. "
        f"Got close.call_count={store.close.call_count}."
    )
    assert mock_orch._client.reset_baselines.call_count == 0, (
        "reset_baselines() must NOT fire when cleanup raised; the per-session "
        "cost baseline belongs to a session whose final state we cannot vouch "
        f"for. Got call_count={mock_orch._client.reset_baselines.call_count}."
    )


@pytest.mark.asyncio
async def test_ws_endpoint_does_not_close_store_when_cleanup_swallowed_save_failure():
    """Regression guard: when handler.cleanup() catches a save exception
    internally and returns normally (production cleanup contract — it
    logs `session.disconnect_save_failed` AND sets
    `self.last_save_failure = exc`), the close_store teardown must NOT
    fire. Tearing down a store whose final save was lost would compound
    the data loss into a permanent state regression.

    The production teardown gate at ``websocket.py:202`` reads
    ``handler.last_save_failure`` (websocket.py:201) alongside
    ``cleanup_failed`` and ``connected_player_ids()``. When
    last_save_failure is not None, the gate skips close_store and emits
    ``ws.room_teardown_skipped slug=… reason=save_failure_swallowed``.
    This test exercises the save-failure branch of that gate.
    """
    room = SessionRoom(slug="slug-wire-cleanup-swallowed", mode=GameMode.SOLO)
    store = MagicMock()
    room.bind_world(snapshot=MagicMock(), store=store)
    mock_orch = _mock_room_with_baseline_tracking(room)

    handler = _PinnedRoomHandler(
        room,
        endpoint_player="alice",
        cleanup_behavior="swallow_save_exception",
    )

    ws = _fake_ws()
    await ws_endpoint(ws, handler)

    assert handler.cleanup_calls == 1, "cleanup() must have been invoked"
    assert handler.last_save_failure is not None, (
        "Test fixture failed to record the simulated save failure"
    )

    # store.save must NOT appear because the simulated cleanup swallowed
    # the save exception and never re-attempted.
    method_names = [c[0] for c in store.method_calls]
    assert "save" not in method_names, (
        "Test invariant: in the swallowed-save scenario, no save() should "
        f"have hit the store mock. Got method_calls={store.method_calls!r}"
    )

    # The load-bearing assertion: close_store must NOT fire when cleanup
    # silently lost the save. Otherwise we lose the final snapshot AND
    # tear down the store, doubling the damage.
    assert store.close.call_count == 0, (
        "Wiring failure: close_store() fired after cleanup() swallowed a "
        "save exception. The final snapshot was lost; tearing down the "
        "canonical store here makes the data loss permanent. "
        f"Got close.call_count={store.close.call_count}."
    )
    assert mock_orch._client.reset_baselines.call_count == 0, (
        "reset_baselines() must not fire when the final save was lost — "
        "the per-session cost baseline belongs to a session that did NOT "
        "cleanly persist its end-state."
    )
