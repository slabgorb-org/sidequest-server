"""Shared helper for tests that call ``_apply_narration_result_to_snapshot``.

Task E.2 of the session-aggregate strangler made ``room: SessionRoom`` a
required keyword-only argument on the apply function — every test caller
needs a ``SessionRoom`` bound to the snapshot it's exercising. This
helper provides the one-liner that builds a room over a repository double
so test files don't have to repeat the boilerplate.

ADR-115 F1: the legacy ``SqliteStore`` save layer is gone. ``room_for``
binds a ``MagicMock(spec=SaveRepository)`` — the room's ``save()`` is
fire-and-forget for the apply-pipeline callers this helper serves (they
assert on the mutated snapshot / OTEL spans, never on a read-back through
the room's store). Tests that DO read persisted state back must construct a
real ``PgSaveRepository`` on a ``migrated_db`` themselves.

Usage:

    from tests._helpers.session_room import room_for

    room = room_for(snap)
    _apply_narration_result_to_snapshot(snap, result, "Sam", room=room, pack=pack)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.game.session import GameSnapshot
from sidequest.server.session_room import SessionRoom


def room_for(snapshot: GameSnapshot, *, slug: str = "test_world") -> SessionRoom:
    """Build a SessionRoom bound to ``snapshot`` over a repository double.

    Idempotent against re-bind (the ``SessionRoom.bind_world`` itself is
    idempotent — second call no-ops). The slug defaults to ``test_world``
    but callers passing a slug-aware snapshot can override.

    The bound store is a ``MagicMock(spec=SaveRepository)``: ``room.save()``
    and ``room.close_store()`` are no-ops, and reads return ``MagicMock``s.
    No production read-back path runs through this helper.
    """
    room = SessionRoom(slug=slug, mode=GameMode.SOLO)
    room.bind_world(snapshot=snapshot, store=MagicMock(spec=SaveRepository))
    return room
