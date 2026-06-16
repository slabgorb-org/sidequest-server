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
from sidequest.game.turn import TurnPhase
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


def build_seal_reconcile_roster(
    snapshot: GameSnapshot,
    playing_player_ids: Iterable[str],
) -> list[TurnStatusEntry]:
    """Roster reflecting the CURRENT seal truth for a (re)connecting peer.

    Story 67-2: a dropped ACTION_REVEAL/TURN_STATUS frame can strand a peer at
    "Composing". On (re)connect we re-derive seal state and send it so the
    strand is self-healing. The wrinkle is phase-dependence:

    - **InputCollection** (barrier still collecting): ``_submitted`` is the
      authoritative set — defer to :func:`build_turn_status_roster`.
    - **Past InputCollection** (barrier fired, turn resolving): ``submit_input``
      / ``recheck_barrier`` (turn.py:96-98,119-122) CLEARED ``_submitted`` on
      the phase transition. Reading it naively here would report every peer
      ``pending`` and flip a sealed table back to "Composing" — the exact
      regression ``player_action.py:560-574`` guards against on its terminal
      broadcast. So we project all playing peers ``submitted`` (the round has
      effectively closed), mirroring that terminal projection.

    **Story 97-2 — membership derives from the DURABLE seated-PC roster
    (``snapshot.player_seats``), not the live ``playing_player_ids``.** After a
    server reload both seats reconnect; at the instant the FIRST reconnector
    lands, its own socket is the only PLAYING peer in the freshly-rebuilt room,
    so ``playing_player_ids`` under-counts and the reconcile reported a solo
    ``0/1`` (server log ``.20260607-090551`` lines 652/668, 863/878).
    ``snapshot.player_seats`` is durable (Postgres, ADR-115) and already knows
    the table is N-seat, so it is the authoritative denominator. It is *also*
    the 45-2 phantom guard: it is written only on ``_chargen_confirmation``
    commit, so a mid-chargen phantom peer has no entry and is excluded for free
    — the live ``playing_player_ids`` is therefore NOT consulted for membership
    (a phantom leaking into it cannot inflate the roster), but is retained as
    the call contract for pre-97-2 callers.

    The numerator is unchanged: ``_submitted`` still drives who reads
    ``submitted`` during InputCollection. (``_submitted`` is runtime-only and
    reconstructed empty on a process reload, so a seal made *before* the reload
    is not recoverable — out of scope, see the story 97-2 deviation.)

    Read-only: never mutates ``_submitted`` or the phase (presence recovery
    must not perturb the barrier it only reports).
    """
    durable_seat_ids = list(snapshot.player_seats.keys())
    base = build_turn_status_roster(snapshot, durable_seat_ids)
    if snapshot.turn_manager.phase == TurnPhase.InputCollection:
        return base
    # Barrier already fired — project the round's terminal all-submitted state.
    return project_all_submitted(base)


def project_all_submitted(roster: list[TurnStatusEntry]) -> list[TurnStatusEntry]:
    """Return a copy of ``roster`` with every entry forced to ``submitted``.

    The round's terminal projection: used when the barrier has fired and the
    runtime ``_submitted`` set is no longer populated (turn.py clears it on the
    phase transition), so a roster rebuilt from ``_submitted`` would read every
    peer ``pending``. Shared by the on-submission barrier_fired broadcast
    (``handlers/player_action.py``) and the on-connect seal reconcile
    (:func:`build_seal_reconcile_roster`) so the two stay in lockstep — a
    divergence here would flip a sealed table back to "Composing". Pure: copies
    via ``model_copy``, never mutates the input entries."""
    return [entry.model_copy(update={"status": "submitted"}) for entry in roster]
