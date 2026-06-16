"""Canonical per-player sealed-letter roster for TURN_STATUS broadcasts.

ADR-036 sealed-letter pacing: every connected tab must agree on who's in the
round and how many of them have sealed. The Python server previously emitted
TURN_STATUS{active,submitted,resolved} as per-player events and let the UI
accumulate them into a roster. Any dropped or out-of-order delivery diverged
the per-tab denominator (host "(1/2)" vs peers "(2/3)" — sq-playtest
2026-05-12). The fix carries the canonical roster on every broadcast so each
tab reconciles to the server's view rather than its local accumulator.
"""

from __future__ import annotations

from collections.abc import Iterable

from sidequest.game.session import GameSnapshot
from sidequest.protocol.messages import TurnStatusEntry
from sidequest.protocol.types import NonBlankString


def build_turn_status_roster(
    snapshot: GameSnapshot,
    playing_player_ids: Iterable[str],
) -> list[TurnStatusEntry]:
    """Build the canonical sealed-letter roster for the current round.

    For each PLAYING player_id, emit one entry:
    - ``character_name`` from ``snapshot.player_seats`` (falls back to the
      player_id when the seat name is empty — a transient state that should
      never persist once PLAYING is reached but is defended here so a stale
      seat doesn't crash the broadcast)
    - ``status="submitted"`` if the player_id is in
      ``snapshot.turn_manager._submitted`` (the runtime barrier set),
      ``"pending"`` otherwise.

    Entries with blank player_id / character_name are skipped — NonBlankString
    would otherwise raise and break the entire TURN_STATUS broadcast.
    """
    submitted: set[str] = object.__getattribute__(snapshot.turn_manager, "_submitted")
    entries: list[TurnStatusEntry] = []
    for pid in playing_player_ids:
        if not pid or not pid.strip():
            continue
        seat_name = (snapshot.player_seats.get(pid) or "").strip() or pid
        if not seat_name.strip():
            continue
        entries.append(
            TurnStatusEntry(
                player_id=NonBlankString(pid),
                character_name=NonBlankString(seat_name),
                status="submitted" if pid in submitted else "pending",
            )
        )
    return entries
