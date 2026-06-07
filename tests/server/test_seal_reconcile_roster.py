"""Connect-time seal reconcile must reflect the *current* seal truth — not
all-pending — so a dropped ACTION_REVEAL/TURN_STATUS can never strand a peer
at "Composing".

Story 67-2. The 2026-05-27 ping-pong (beneath_sunden MP) caught a peer pinned
on "Adam Composing… (1/2)" after the single ACTION_REVEAL{submitted} +
TURN_STATUS{submitted} frame that flips a peer to "✓ Sealed" raced a transient
socket state and never arrived. The turn had already resolved server-side —
this is a *presence-display* recovery bug, not a barrier bug.

The recovery substrate (``build_turn_status_roster``) reports seal state from
``turn_manager._submitted`` while the barrier is still collecting. But the
moment the barrier fires, ``submit_input``/``recheck_barrier`` CLEAR
``_submitted`` (turn.py:98,122) and advance the phase past InputCollection. A
peer who reconnects *after* the barrier fired (turn resolving) would then read
an EMPTY ``_submitted`` and see every peer flip back to "pending" — the exact
regression player_action.py:560-574 already guards against on its terminal
broadcast. The reconcile builder must apply that same terminal projection so a
sealed peer is never regressed to pending on reconnect.

These tests pin the NEW reconcile builder. Proposed contract:
``build_seal_reconcile_roster(snapshot, playing_player_ids)`` in
``sidequest.server.turn_status_roster`` — phase-aware: defer to ``_submitted``
during InputCollection, project all-submitted once the barrier has fired. (The
name is a proposed TDD contract; the load-bearing assertion is the behavior,
not the symbol — see the connect wiring test for the refactor-stable proof.)
"""

from __future__ import annotations

from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnPhase
from sidequest.server.turn_status_roster import build_seal_reconcile_roster


def _snapshot_with_seats(seats: dict[str, str]) -> GameSnapshot:
    snapshot = GameSnapshot()
    snapshot.player_seats.update(seats)
    return snapshot


def _submitted_set(snapshot: GameSnapshot) -> set[str]:
    return object.__getattribute__(snapshot.turn_manager, "_submitted")


# ---------------------------------------------------------------------------
# AC1 — a dropped seal frame does not strand a peer: reconcile reports the
# already-sealed peer as submitted while the barrier is still collecting.
# ---------------------------------------------------------------------------


def test_reconcile_marks_sealed_peer_submitted_during_input_collection() -> None:
    """Adam sealed (1/2), Eve still composing, barrier still collecting.
    Eve's reconnect reconcile must show Adam ``submitted`` and Eve ``pending``
    — the seal fact recovered from ``_submitted`` without any ACTION_REVEAL."""
    snapshot = _snapshot_with_seats({"adam": "Adam", "eve": "Eve"})
    snapshot.turn_manager.phase = TurnPhase.InputCollection
    snapshot.turn_manager.player_count = 2
    _submitted_set(snapshot).add("adam")

    roster = build_seal_reconcile_roster(snapshot, ["adam", "eve"])

    by_id = {e.player_id.as_str(): e.status for e in roster}
    assert by_id == {"adam": "submitted", "eve": "pending"}, (
        "reconcile during InputCollection must mirror _submitted exactly — "
        "Adam sealed, Eve not"
    )


# ---------------------------------------------------------------------------
# AC1 edge / SM-flagged assumption — barrier already fired: _submitted is
# cleared, phase is past InputCollection. Reconcile must NOT regress sealed
# peers to pending. This is the load-bearing edge the context calls out.
# ---------------------------------------------------------------------------


def test_reconcile_projects_all_submitted_after_barrier_fired() -> None:
    """After the barrier fires (turn resolving), turn.py clears ``_submitted``
    and advances the phase. A peer reconnecting now must see every playing
    peer ``submitted`` (terminal projection), NOT all-pending — mirrors the
    barrier_fired branch in player_action.py:568-574. Reading ``_submitted``
    naively here would flip a sealed table back to 'Composing' — the exact
    strand this story kills."""
    snapshot = _snapshot_with_seats({"adam": "Adam", "eve": "Eve"})
    # Simulate post-fire state: submit_input cleared _submitted on the
    # transition to IntentRouting (turn.py:96-98).
    snapshot.turn_manager.phase = TurnPhase.IntentRouting
    snapshot.turn_manager.player_count = 2
    assert _submitted_set(snapshot) == set(), "precondition: barrier fire cleared _submitted"

    roster = build_seal_reconcile_roster(snapshot, ["adam", "eve"])

    statuses = {e.player_id.as_str(): e.status for e in roster}
    assert statuses == {"adam": "submitted", "eve": "submitted"}, (
        "once the barrier has fired the round is resolving — every playing "
        "peer has effectively sealed; reconcile must project all-submitted "
        "instead of reading the now-empty _submitted set as all-pending"
    )


def test_reconcile_three_player_partial_seal_denominator() -> None:
    """3-player room, 2 of 3 sealed, still collecting. Reconcile must report
    exactly the two sealers as submitted so the per-tab '(2/3)' denominator is
    correct on reconnect (off-by-one roster denominators were the prior-art
    bug class — context AC edge cases)."""
    snapshot = _snapshot_with_seats({"a": "Ann", "b": "Bea", "c": "Cy"})
    snapshot.turn_manager.phase = TurnPhase.InputCollection
    snapshot.turn_manager.player_count = 3
    _submitted_set(snapshot).update({"a", "b"})

    roster = build_seal_reconcile_roster(snapshot, ["a", "b", "c"])

    by_id = {e.player_id.as_str(): e.status for e in roster}
    assert by_id == {"a": "submitted", "b": "submitted", "c": "pending"}
    assert sum(1 for e in roster if e.status == "submitted") == 2


# ---------------------------------------------------------------------------
# AC6 — recovery is display-only: building the reconcile roster must NEVER
# mutate barrier state (phase / _submitted). A read that advanced the barrier
# would double-dispatch or shorten Alex's window.
# ---------------------------------------------------------------------------


def test_reconcile_is_read_only_and_does_not_touch_the_barrier() -> None:
    """The reconcile builder is a pure read of seal truth. It must not add to,
    clear, or otherwise mutate ``_submitted`` and must not advance the phase —
    otherwise presence recovery would perturb the barrier it is only meant to
    *report* (SOUL: never weaken the barrier; do not shorten the window)."""
    snapshot = _snapshot_with_seats({"adam": "Adam", "eve": "Eve"})
    snapshot.turn_manager.phase = TurnPhase.InputCollection
    snapshot.turn_manager.player_count = 2
    _submitted_set(snapshot).add("adam")

    before_phase = snapshot.turn_manager.phase
    before_submitted = set(_submitted_set(snapshot))

    build_seal_reconcile_roster(snapshot, ["adam", "eve"])
    # Build a second time — idempotent, still no mutation.
    build_seal_reconcile_roster(snapshot, ["adam", "eve"])

    assert snapshot.turn_manager.phase == before_phase, "reconcile must not advance the phase"
    assert _submitted_set(snapshot) == before_submitted, (
        "reconcile must not mutate the runtime _submitted barrier set — "
        "presence recovery is read-only"
    )
