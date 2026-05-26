"""Pingpong 2026-04-30 follow-on bug: dispatch-winner stuck after merged narration.

Repro: 4P MP, the LAST submitter's WS connection cycles during the long
(30-60s) Claude narration await. The dispatcher's pre-await `socket_id` is
captured in the merged-dispatch closure; the peer-only broadcast added in
4b90250 used `exclude_socket_id=self._socket_id` to skip the dispatcher
"because they receive it through the original outbound queue." But:
- The dispatcher's writer task is cancelled when their pre-await socket
  disconnects, so messages put on `outbound` are silently dropped.
- The post-broadcast envelopes (NARRATION_END / CHAPTER_MARKER / PARTY_STATUS
  / AUDIO_CUE) are NOT in the EventLog, so reconnect replay can't backfill
  them. Last-submitter froze on the prior turn's "Waiting on…" banner with
  disabled input until they hard-reloaded.

Fix: emit shared-world frames via a single broadcast to every CURRENT
socket in the room (`exclude_socket_id=None`). Single delivery path —
peers and dispatcher both receive the frame on whatever socket is
currently registered. Eliminates the double-delivery hazard the prior
peer-only-plus-outbound pattern would have introduced if we'd switched
to broadcast-to-all without removing `outbound.append`.

This is a wiring test — drives the actual `_execute_narration_turn`
handler against a real `SessionRoom` with four connected sockets and
asserts EVERY socket's outbound queue receives the four shared-world
frames after a merged dispatch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.sqlite_repository import SqliteSaveRepository
from sidequest.protocol.messages import (
    ChapterMarkerMessage,
    NarrationEndMessage,
    PartyStatusMessage,
)
from sidequest.server.session_room import RoomRegistry

_SLUG = "shared-world-frame-broadcast-test"


def _seed_game_row(tmp_path: Path) -> SqliteStore:
    db = db_path_for_slug(tmp_path, _SLUG)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=_SLUG,
        mode=GameMode.MULTIPLAYER,
        genre_slug="caverns_and_claudes",
        world_slug="",
    )
    return store


async def _drain(queue: asyncio.Queue[object]) -> list[object]:
    out: list[object] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


@pytest.mark.asyncio
async def test_shared_world_frames_reach_every_socket_including_dispatcher(
    session_handler_factory,
    tmp_path: Path,
) -> None:
    """4-player MP regression: every connected socket — INCLUDING the
    dispatch winner's — receives NARRATION_END, CHAPTER_MARKER, and
    PARTY_STATUS after the merged narration completes.

    Pre-fix (4b90250): peer-only broadcast excluded `self._socket_id`,
    relying on `outbound.append` to deliver to the dispatcher. When the
    dispatcher's WS cycled mid-narration the outbound delivery silently
    failed; the four envelopes aren't in EventLog, so reconnect replay
    couldn't backfill them — last-submitter stuck.

    Post-fix: single `room.broadcast(exclude_socket_id=None)` path
    delivers to every CURRENT socket in the room, so the dispatcher's
    queue (or their reconnected queue) gets the frame regardless of
    socket lifecycle.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "linus"
    sd.player_name = "Linus"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG

    store = _seed_game_row(tmp_path)
    repo = SqliteSaveRepository(store)
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)

    # 4-socket multiplayer room. Linus is the dispatch winner (last
    # submitter); Charlie/Snoopy/Lucy are peers. Each socket has its own
    # asyncio.Queue.
    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    socket_ids = {
        "linus": "sock-linus",
        "charlie": "sock-charlie",
        "snoopy": "sock-snoopy",
        "lucy": "sock-lucy",
    }
    queues: dict[str, asyncio.Queue[object]] = {pid: asyncio.Queue() for pid in socket_ids}
    for pid, sid in socket_ids.items():
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, queues[pid])
    handler._room = room
    handler._socket_id = socket_ids["linus"]

    # Orchestrator mock: returns a narration with a location so the
    # CHAPTER_MARKER branch fires. No confrontation, no audio cue
    # (audio dispatch short-circuits without a configured backend).
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="The four crew approach the doorway in lock-step.",
            location="Vaskov Centrum — Inspector Karenina's Office",
        ),
    )

    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "Linus: I push the door open one full panel-width.",
        _build_turn_context(sd),
    )

    # The dispatcher's `outbound` list (function return value) should NOT
    # contain the broadcast frames anymore — they were redirected to
    # `room.broadcast(exclude_socket_id=None)` so the same delivery path
    # serves both dispatcher and peers.
    outbound_kinds = [type(m).__name__ for m in msgs]
    assert NarrationEndMessage.__name__ not in outbound_kinds, (
        f"NARRATION_END must NOT be in the dispatcher's outbound list — "
        f"it's broadcast via room.broadcast now. Got outbound: {outbound_kinds}"
    )
    assert ChapterMarkerMessage.__name__ not in outbound_kinds, (
        f"CHAPTER_MARKER must NOT be in the dispatcher's outbound list — "
        f"it's broadcast via room.broadcast now. Got outbound: {outbound_kinds}"
    )
    assert PartyStatusMessage.__name__ not in outbound_kinds, (
        f"PARTY_STATUS must NOT be in the dispatcher's outbound list — "
        f"it's broadcast via room.broadcast now. Got outbound: {outbound_kinds}"
    )

    # Every socket queue — including the dispatcher's (Linus) — must
    # receive each shared-world frame. This is the load-bearing assertion:
    # pre-fix, Linus's queue was empty for these frames, his `canType`
    # never flipped back, and the input bar stayed sealed.
    for pid in socket_ids:
        frames = await _drain(queues[pid])
        narration_ends = [f for f in frames if isinstance(f, NarrationEndMessage)]
        chapter_markers = [f for f in frames if isinstance(f, ChapterMarkerMessage)]
        party_statuses = [f for f in frames if isinstance(f, PartyStatusMessage)]
        assert len(narration_ends) == 1, (
            f"Socket for {pid!r} expected exactly one NARRATION_END frame "
            f"on their queue; got {len(narration_ends)}. "
            f"Frames on queue: {[type(f).__name__ for f in frames]}. "
            "If pid='linus', this is the dispatch-winner-stuck regression "
            "(pingpong 2026-04-30 follow-on)."
        )
        assert len(chapter_markers) == 1, (
            f"Socket for {pid!r} expected exactly one CHAPTER_MARKER frame; "
            f"got {len(chapter_markers)}. Running-header chapter title would "
            f"stay blank without this frame on the dispatcher's tab."
        )
        assert len(party_statuses) == 1, (
            f"Socket for {pid!r} expected exactly one PARTY_STATUS frame; "
            f"got {len(party_statuses)}. Party panel would freeze at the "
            f"pre-narration state without this frame."
        )


@pytest.mark.asyncio
async def test_solo_room_dispatcher_still_receives_shared_world_frames(
    session_handler_factory,
    tmp_path: Path,
) -> None:
    """Solo regression: with only one socket in the room, the broadcast
    must still deliver — `exclude_socket_id=None` includes the lone
    socket. Guards against accidentally re-introducing peer-only
    semantics that would silently strand solo players.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "solo"
    sd.player_name = "Solo"
    sd.mode = GameMode.SOLO
    sd.game_slug = _SLUG + "-solo"

    store = _seed_game_row(tmp_path)
    repo = SqliteSaveRepository(store)
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)

    registry = RoomRegistry()
    room = registry.get_or_create(slug=sd.game_slug, mode=GameMode.SOLO)
    sid = "sock-solo"
    queue: asyncio.Queue[object] = asyncio.Queue()
    room.connect("solo", socket_id=sid)
    room.attach_outbound(sid, queue)
    handler._room = room
    handler._socket_id = sid

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="A door opens.",
            location="The Threshold",
        ),
    )

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(
        sd,
        "I step through.",
        _build_turn_context(sd),
    )

    frames = await _drain(queue)
    narration_ends = [f for f in frames if isinstance(f, NarrationEndMessage)]
    chapter_markers = [f for f in frames if isinstance(f, ChapterMarkerMessage)]
    party_statuses = [f for f in frames if isinstance(f, PartyStatusMessage)]
    assert len(narration_ends) == 1, (
        f"Solo socket must still receive NARRATION_END via the unified "
        f"broadcast path; got {len(narration_ends)}."
    )
    assert len(chapter_markers) == 1, (
        f"Solo socket must still receive CHAPTER_MARKER; got {len(chapter_markers)}."
    )
    assert len(party_statuses) == 1, (
        f"Solo socket must still receive PARTY_STATUS; got {len(party_statuses)}."
    )
