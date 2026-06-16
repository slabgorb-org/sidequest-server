"""Unit tests for Task 14 — folded/out table seats drop from the barrier denominator.

These four tests cover the ``_table_folded_player_ids`` parallel set on ``SessionRoom``:

1. A folded player drops from the denominator immediately.
2. A table fold SURVIVES ``drain_pending_actions`` — the load-bearing distinction from
   crash-release: drain clears ``_crash_released`` but must NOT clear
   ``_table_folded_player_ids`` (table folds span multiple decision points within
   one hand, while crash-release is scoped to a single interaction).
3. ``clear_table_folds()`` restores the denominator to full at table teardown.
4. ``mark_table_folded`` is idempotent — marking the same player twice does not
   double-reduce the denominator.

Real seating API (confirmed against existing barrier tests in
``test_67_1_crash_signal_barrier_release.py`` and
``test_mp_turn_barrier_active_turn_count.py``):
  - ``SessionRoom(slug=..., mode=GameMode.MULTIPLAYER)``
  - ``room.seat(player_id, character_slot=...)``  → state = CHARGEN
  - ``room._seated[player_id].state = LobbyState.PLAYING``  → promote to PLAYING
"""

from __future__ import annotations

from sidequest.game.persistence import GameMode
from sidequest.server.session_room import LobbyState, SessionRoom


def _room_with_players(n: int) -> SessionRoom:
    """Create a ``SessionRoom`` with *n* PLAYING peers.

    Uses the canonical pattern from the existing barrier/crash tests:
    ``seat(...)`` then direct ``_seated[pid].state = LobbyState.PLAYING``.
    """
    room = SessionRoom(slug="table-test", mode=GameMode.MULTIPLAYER)
    for i in range(1, n + 1):
        pid = f"p{i}"
        room.seat(pid, character_slot=pid)
        room._seated[pid].state = LobbyState.PLAYING  # noqa: SLF001
    return room


def test_folded_player_drops_from_denominator():
    """AC1: marking a player as table-folded lowers the barrier denominator by 1."""
    room = _room_with_players(3)
    assert room.effective_barrier_count() == 3
    room.mark_table_folded("p2")
    assert room.effective_barrier_count() == 2


def test_table_fold_survives_interaction_drain():
    """AC2 (load-bearing): drain_pending_actions clears crash-release but MUST NOT
    clear table folds.  A fold is committed for the rest of the hand (multiple
    decision points), so it must persist across the per-interaction drain boundary.
    """
    room = _room_with_players(3)
    room.mark_table_folded("p2")
    room.drain_pending_actions()  # clears _crash_released; must NOT clear table folds
    assert room.effective_barrier_count() == 2, (
        "Table fold must survive drain_pending_actions. "
        "If this is 3, _table_folded_player_ids was (incorrectly) cleared in drain."
    )


def test_clear_table_folds_restores_denominator():
    """AC3: clear_table_folds() at table teardown restores the full denominator."""
    room = _room_with_players(3)
    room.mark_table_folded("p2")
    room.clear_table_folds()
    assert room.effective_barrier_count() == 3


def test_mark_table_folded_is_idempotent():
    """AC4: marking the same player folded twice must not double-reduce the count."""
    room = _room_with_players(3)
    room.mark_table_folded("p2")
    room.mark_table_folded("p2")
    assert room.effective_barrier_count() == 2


def test_player_in_both_release_sets_counted_once():
    """A player who is BOTH crash-released AND table-folded must be subtracted
    ONCE — effective_barrier_count unions the two sets before counting, so the
    same player_id in both does not double-reduce the denominator.
    """
    room = _room_with_players(3)
    room.mark_crash_released("p2")
    room.mark_table_folded("p2")  # same player in both sets
    assert room.effective_barrier_count() == 2  # subtracted once, not twice
