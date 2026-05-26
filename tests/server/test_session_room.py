from unittest.mock import MagicMock

import pytest

from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.server.session_room import RoomRegistry, SessionRoom, SoloSlotConflict


def test_room_registry_returns_same_room_for_same_slug():
    reg = RoomRegistry()
    r1 = reg.get_or_create("slug-a", mode=GameMode.MULTIPLAYER)
    r2 = reg.get_or_create("slug-a", mode=GameMode.MULTIPLAYER)
    assert r1 is r2


def test_room_tracks_connected_players():
    room = SessionRoom(slug="slug-a", mode=GameMode.MULTIPLAYER)
    room.connect("alice", socket_id="sock-1")
    room.connect("bob", socket_id="sock-2")
    assert set(room.connected_player_ids()) == {"alice", "bob"}


def test_room_disconnect_removes_player():
    room = SessionRoom(slug="slug-a", mode=GameMode.MULTIPLAYER)
    room.connect("alice", socket_id="sock-1")
    room.disconnect(socket_id="sock-1")
    assert room.connected_player_ids() == []


def test_same_player_reconnect_updates_socket():
    room = SessionRoom(slug="slug-a", mode=GameMode.MULTIPLAYER)
    room.connect("alice", socket_id="sock-1")
    room.connect("alice", socket_id="sock-2")
    room.disconnect(socket_id="sock-1")
    assert "alice" in room.connected_player_ids()  # sock-2 still holds alice


def test_solo_room_rejects_second_connection():
    room = SessionRoom(slug="slug-a", mode=GameMode.SOLO)
    room.connect("alice", socket_id="sock-1")
    with pytest.raises(SoloSlotConflict):
        room.connect("bob", socket_id="sock-2")


def test_solo_room_allows_same_player_reconnect():
    room = SessionRoom(slug="slug-a", mode=GameMode.SOLO)
    room.connect("alice", socket_id="sock-1")
    room.disconnect(socket_id="sock-1")
    room.connect("alice", socket_id="sock-2")  # must not raise
    assert room.connected_player_ids() == ["alice"]


def test_seated_players_separate_from_connected():
    room = SessionRoom(slug="slug-a", mode=GameMode.MULTIPLAYER)
    room.seat("alice", character_slot="rux")
    room.seat("bob", character_slot="vex")
    room.connect("alice", socket_id="sock-1")
    # bob seated but not connected
    assert set(room.seated_player_ids()) == {"alice", "bob"}
    assert set(room.connected_player_ids()) == {"alice"}
    assert set(room.absent_seated_player_ids()) == {"bob"}


def test_slot_to_player_id_returns_seat_map():
    room = SessionRoom(slug="slug-a", mode=GameMode.MULTIPLAYER)
    room.seat("alice", character_slot="Laverne")
    room.seat("bob", character_slot="Shirley")
    assert room.slot_to_player_id() == {"Laverne": "alice", "Shirley": "bob"}


def test_slot_to_player_id_skips_seats_without_slot():
    room = SessionRoom(slug="slug-a", mode=GameMode.MULTIPLAYER)
    room.seat("alice", character_slot=None)  # legacy / pre-slot seat
    room.seat("bob", character_slot="Shirley")
    assert room.slot_to_player_id() == {"Shirley": "bob"}


def test_slot_to_player_id_empty_when_no_seats():
    room = SessionRoom(slug="slug-a", mode=GameMode.MULTIPLAYER)
    assert room.slot_to_player_id() == {}


# ---------------------------------------------------------------------------
# Canonical snapshot binding (ADR-037 Python port)
# ---------------------------------------------------------------------------


def _fresh_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        location="Entrance",
    )


def test_bind_world_sets_snapshot_and_store_once():
    """First bind populates both fields; getters reflect them."""
    room = SessionRoom(slug="2026-04-25-test-mp", mode=GameMode.MULTIPLAYER)
    snap = _fresh_snapshot()
    store = MagicMock()

    assert room.snapshot is None
    assert room.store is None

    room.bind_world(snapshot=snap, store=store)

    assert room.snapshot is snap
    assert room.store is store


def test_bind_world_is_idempotent():
    """Second bind when already populated is a no-op (no overwrite, no raise).

    Guards against a race where two concurrent first-connects both try to
    bind. The first wins; the second silently observes the existing
    binding rather than stomping it.
    """
    room = SessionRoom(slug="slug", mode=GameMode.MULTIPLAYER)
    snap1 = _fresh_snapshot()
    store1 = MagicMock()
    snap2 = _fresh_snapshot()
    store2 = MagicMock()

    room.bind_world(snapshot=snap1, store=store1)
    room.bind_world(snapshot=snap2, store=store2)

    assert room.snapshot is snap1
    assert room.store is store1


def test_close_store_is_idempotent_and_calls_close_once():
    """close_store closes the bound store exactly once across N calls."""
    room = SessionRoom(slug="slug", mode=GameMode.MULTIPLAYER)
    store = MagicMock()
    room.bind_world(snapshot=_fresh_snapshot(), store=store)

    room.close_store()
    room.close_store()

    assert store.close.call_count == 1


def test_close_store_when_unbound_is_noop():
    """Pre-bind / never-bound rooms must not raise on close."""
    room = SessionRoom(slug="slug", mode=GameMode.MULTIPLAYER)
    room.close_store()  # must not raise


def test_close_store_unbinds_snapshot_so_next_bind_reattaches_store():
    """Regression: after close_store, a fresh bind_world must re-attach
    both snapshot AND store.

    Before this fix, close_store nulled only ``_store`` and left
    ``_snapshot`` set. The next ``bind_world`` then early-returned on
    its ``_snapshot is not None`` idempotency check WITHOUT re-attaching
    a store, leaving ``room.store is None``. Every reconnecting session
    built ``_SessionData(store=None)`` and crashed at the first
    ``sd.store.recent_narrative(...)`` call (sq-playtest 2026-05-24
    on dust_and_lead-mp after last-disconnect → reconnect).

    The contract: close_store is a full teardown. After it, the room
    behaves as if never bound — the next bind_world goes through the
    full attachment path.
    """
    room = SessionRoom(slug="slug-recycle", mode=GameMode.MULTIPLAYER)

    snap1 = _fresh_snapshot()
    store1 = MagicMock()
    room.bind_world(snapshot=snap1, store=store1)
    assert room.snapshot is snap1
    assert room.store is store1

    room.close_store()
    assert room.snapshot is None, (
        "close_store must null _snapshot so the next bind_world re-binds "
        "fully (not early-returns on stale snapshot)"
    )
    assert room.store is None

    # Second bind — represents a reconnecting session after everyone left.
    snap2 = _fresh_snapshot()
    store2 = MagicMock()
    room.bind_world(snapshot=snap2, store=store2)

    assert room.snapshot is snap2, "re-bind must attach the fresh snapshot"
    assert room.store is store2, (
        "re-bind must attach the fresh store — this is the regression: a "
        "reconnect after close_store left store=None and broke turn_context"
    )


def test_close_store_resets_narrator_cost_baselines():
    """Story 61-4 (Architect spec-check A) wiring test.

    ``RoomRegistry`` never evicts a slug (session_room.py: get_or_create
    only ever inserts), so the ``AnthropicSdkClient`` backing the slug's
    orchestrator lives for the server process lifetime — without a reset
    on slug recycle, the rolling cost baseline can self-train onto a
    sustained runaway and silence the alarm. ``SessionRoom.close_store``
    is the slug-recycle seam; it MUST invoke ``reset_baselines()`` on
    the orchestrator's client when present.
    """
    room = SessionRoom(slug="slug", mode=GameMode.MULTIPLAYER)
    room.bind_world(snapshot=_fresh_snapshot(), store=MagicMock())

    # Stand in a fake orchestrator carrying a client with reset_baselines.
    fake_client = MagicMock()
    fake_orch = MagicMock()
    fake_orch._client = fake_client
    room._orchestrator = fake_orch  # noqa: SLF001 — direct seed for the test

    room.close_store()

    # Story 61-followup-A — reset_baselines now requires a session_id.
    # Assert the exact call shape (slug-as-session_id), not just call_count;
    # the old call_count-only assertion let a zero-arg reset() slip past
    # because MagicMock accepts any arity. The slug is the canonical
    # session_id (it flows down to AnthropicSdkClient as
    # session_id=sd.game_slug via session_helpers.py:998).
    fake_client.reset_baselines.assert_called_once_with("slug")


def test_close_store_tolerates_client_without_reset_baselines():
    """Non-SDK backends (claude -p, Ollama) don't expose reset_baselines.
    close_store MUST no-op rather than crash teardown on those clients."""
    room = SessionRoom(slug="slug", mode=GameMode.MULTIPLAYER)
    room.bind_world(snapshot=_fresh_snapshot(), store=MagicMock())

    # Build a client object with NO reset_baselines attribute.
    class _BareClient:
        pass

    fake_orch = MagicMock()
    fake_orch._client = _BareClient()
    room._orchestrator = fake_orch  # noqa: SLF001

    # Must not raise.
    room.close_store()


# ---------------------------------------------------------------------------
# Disconnect-save store-lifecycle invariant
# (playtest 2026-04-25 [BUG-LOW] "Cannot operate on a closed database")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_does_not_close_room_owned_store(tmp_path):
    """When the session is bound to a room, ``cleanup()`` must NOT close
    the underlying SqliteStore — the room owns the store lifecycle and
    the same store is shared with every other session bound to the slug.
    Closing it from one cleanup leaves ``room.save()`` operating on a
    closed connection from any other path's perspective and produces
    ``session.disconnect_save_failed error=Cannot operate on a closed
    database``.

    Regression for playtest 2026-04-25 [BUG-LOW]. The bug was triggered
    by trigger-the-confrontation-crash → return to lobby → server
    cleanup of the prior WS — the per-session ``store.close()`` ran in
    the finally block of cleanup, but the same store reference was
    reachable through the room's ``room.save()`` path (e.g., from a
    later turn-end save on a sibling connection or a disconnect-save
    from a peer player in MP).
    """
    from unittest.mock import AsyncMock, MagicMock

    from sidequest.game.persistence import SqliteStore
    from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData

    handler = WebSocketSessionHandler(save_dir=tmp_path)
    snap = _fresh_snapshot()
    store = SqliteStore.open_in_memory()

    room = SessionRoom(slug="slug-cleanup", mode=GameMode.SOLO)
    room.bind_world(snapshot=snap, store=store)

    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name="Rux",
        player_id="player-1",
        snapshot=snap,
        repository=store,  # same store reference the room holds
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(run_narration_turn=AsyncMock()),
    )
    handler._session_data = sd
    handler._room = room

    await handler.cleanup()

    # The underlying SQLite connection must still be open — the room
    # owns the lifecycle, not the per-session cleanup.
    assert room.store is store, "room.store reference must not be None'd by cleanup"
    # Operating on the store after cleanup must succeed. If cleanup
    # closed the connection, ``room.save()`` raises sqlite3.ProgrammingError.
    room.save()  # must not raise


@pytest.mark.asyncio
async def test_cleanup_closes_per_session_store_when_no_room(tmp_path):
    """Legacy non-slug path: ``cleanup()`` must still close the
    per-session store when no room is bound — the session owns the
    store lifecycle in that path. Preserves the original close-on-
    disconnect behavior for any code path that hasn't been migrated to
    the room model.
    """
    from unittest.mock import AsyncMock, MagicMock

    from sidequest.game.persistence import SqliteStore
    from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData

    handler = WebSocketSessionHandler(save_dir=tmp_path)
    store = SqliteStore.open_in_memory()
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name="Rux",
        player_id="player-1",
        snapshot=_fresh_snapshot(),
        repository=store,
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(run_narration_turn=AsyncMock()),
    )
    handler._session_data = sd
    handler._room = None  # explicit: legacy non-slug path

    await handler.cleanup()

    # The store WAS closed — non-slug path owns its own store. Operating
    # on it now should raise.
    import sqlite3

    with pytest.raises(sqlite3.ProgrammingError):
        store.save(_fresh_snapshot())


# ---------------------------------------------------------------------------
# broadcast() return value — pingpong 2026-04-30 "Scrapbook only on host"
# regression. Prior code computed `recipients_count = len(connected_player_ids)`
# which over-reports whenever `_outbound_queues` and `_connected` diverge
# (peer's WebSocket closed without `detach_outbound` running yet, or
# `attach_outbound` hasn't fired post-connect). The IMAGE broadcast log
# claimed `recipients=4` while only the host's queue actually received the
# message; peers silently missed the IMAGE. broadcast() now returns the
# list of (socket_id, player_id) pairs actually queued onto so the GM panel
# can diff that ground truth against `_connected` and surface the gap.
# ---------------------------------------------------------------------------


def test_broadcast_returns_only_sockets_with_attached_outbound_queues():
    """`_outbound_queues` is the ground truth for delivery. If a peer is in
    `_connected` (via `room.connect`) but their `attach_outbound` hasn't
    fired yet (race) or `detach_outbound` already ran (disconnect cleanup),
    they MUST NOT appear in the broadcast return value — and `len()` of the
    return must be the lie-detector recipient count, not `len(_connected)`.
    """
    import asyncio

    room = SessionRoom(slug="slug-broadcast", mode=GameMode.MULTIPLAYER)
    # Four players in `_connected` — but only three have attached queues.
    # Mirrors the production race where a peer's WebSocket dropped after
    # `room.connect` but before/around `attach_outbound`.
    room.connect("charlie", socket_id="sock-charlie")
    room.connect("snoopy", socket_id="sock-snoopy")
    room.connect("linus", socket_id="sock-linus")
    room.connect("lucy", socket_id="sock-lucy")
    queues: dict[str, asyncio.Queue[object]] = {
        sid: asyncio.Queue() for sid in ("sock-charlie", "sock-snoopy", "sock-linus")
    }
    for sid, q in queues.items():
        room.attach_outbound(sid, q)

    delivered = room.broadcast({"type": "IMAGE"}, exclude_socket_id=None)

    assert len(delivered) == 3, (
        f"Expected exactly 3 delivered recipients (only sockets with attached "
        f"outbound queues); got {len(delivered)}. `connected_player_ids()` = "
        f"{room.connected_player_ids()} should NOT be the source of truth — "
        f"this is the divergence the pingpong 2026-04-30 IMAGE bug exploited."
    )
    delivered_socket_ids = {sid for sid, _pid in delivered}
    assert delivered_socket_ids == {"sock-charlie", "sock-snoopy", "sock-linus"}, (
        f"Delivered socket ids must match `_outbound_queues` keys, not "
        f"`_connected` values; got {delivered_socket_ids}."
    )
    delivered_player_ids = {pid for _sid, pid in delivered if pid is not None}
    assert delivered_player_ids == {"charlie", "snoopy", "linus"}, (
        f"Per-recipient player_id resolution must come from `_sockets`; got {delivered_player_ids}."
    )

    # Lucy was in `_connected` but had no outbound queue — she got nothing.
    # `connected_player_ids` over-reports compared to actual delivery; this
    # is the gap the new OTEL `scrapbook_image_broadcast.queue_connect_divergence`
    # field surfaces to the GM panel.
    assert "lucy" not in delivered_player_ids
    assert "lucy" in room.connected_player_ids()


def test_broadcast_excludes_socket_returns_remaining_recipients():
    """`exclude_socket_id` filtering applies to the return value too —
    callers using the return value as the recipient ledger see the same
    list the queues received."""
    import asyncio

    room = SessionRoom(slug="slug-exclude", mode=GameMode.MULTIPLAYER)
    room.connect("alice", socket_id="sock-alice")
    room.connect("bob", socket_id="sock-bob")
    qa: asyncio.Queue[object] = asyncio.Queue()
    qb: asyncio.Queue[object] = asyncio.Queue()
    room.attach_outbound("sock-alice", qa)
    room.attach_outbound("sock-bob", qb)

    delivered = room.broadcast({"x": 1}, exclude_socket_id="sock-alice")
    assert len(delivered) == 1
    assert delivered[0][0] == "sock-bob"
    assert delivered[0][1] == "bob"
    # Alice's queue must stay empty.
    assert qa.empty()
    # Bob's queue got the frame.
    assert qb.qsize() == 1


def test_broadcast_returns_empty_list_when_no_sockets_attached():
    """No outbound queues attached → no recipients. Prior code logged
    `recipients=N` (== `len(_connected)`) even when zero queues received the
    message; new return-value path makes the empty case explicit."""
    room = SessionRoom(slug="slug-empty", mode=GameMode.MULTIPLAYER)
    room.connect("alice", socket_id="sock-alice")
    # No attach_outbound call — alice is in `_connected` but no queue.

    delivered = room.broadcast({"x": 1}, exclude_socket_id=None)
    assert delivered == []
    assert room.connected_player_ids() == ["alice"]  # _connected still says 1
