"""Shared helper for tests that call ``_apply_narration_result_to_snapshot``.

Task E.2 of the session-aggregate strangler made ``room: SessionRoom`` a
required keyword-only argument on the apply function — every test caller
needs a ``SessionRoom`` bound to the snapshot it's exercising. This
helper provides the one-liner that builds a room over an in-memory
SqliteStore so test files don't have to repeat the boilerplate.

Usage:

    from tests._helpers.session_room import room_for

    room = room_for(snap)
    _apply_narration_result_to_snapshot(snap, result, "Sam", room=room, pack=pack)
"""

from __future__ import annotations

from sidequest.game.persistence import GameMode, SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.server.session_room import SessionRoom


def room_for(snapshot: GameSnapshot, *, slug: str = "test_world") -> SessionRoom:
    """Build a SessionRoom bound to ``snapshot`` over an in-memory store.

    Idempotent against re-bind (the ``SessionRoom.bind_world`` itself is
    idempotent — second call no-ops). The slug defaults to ``test_world``
    but callers passing a slug-aware snapshot can override.
    """
    room = SessionRoom(slug=slug, mode=GameMode.SOLO)
    room.bind_world(snapshot=snapshot, store=SqliteStore.open_in_memory())
    return room
