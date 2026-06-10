"""Story 97-2: the seal-reconcile roster denominator must derive from the
DURABLE seated-PC roster (``snapshot.player_seats``), not the live-socket set.

Measured 3x in server log ``.20260607-090551`` (lines 652/668, 863/878), always
the first reconnector: after a server reload both seats reconnect; the FIRST
reconnector gets ``turn_status.reconciled_on_connect sealed=0/1`` — a *solo*
roster — instead of ``0/2``. Root cause: ``build_seal_reconcile_roster`` builds
its roster from ``room.playing_player_ids()`` (the live sockets), and at the
instant the first reconnector lands its own socket is the only PLAYING peer in
the freshly-rebuilt room. The durable ``snapshot.player_seats`` already knows
the table is 2-seat — reconcile against it.

Two coupled truths drive these tests:

1. ``snapshot.player_seats`` is durable (Postgres-persisted, ADR-115) and is
   populated by ``_chargen_confirmation`` ON COMMIT only — so it contains every
   *committed* PC and NO mid-chargen phantom. That makes it the correct
   denominator source AND the natural 45-2 phantom-peer guard in one.
2. ``turn_manager._submitted`` is runtime-only (``turn.py`` ``model_post_init``;
   never serialized) — so a true server-process reload reconstructs it EMPTY.
   The recoverable bug is therefore the DENOMINATOR (roster size), not the
   numerator (who sealed). The numerator-across-reload edge is tracked as a TEA
   design deviation, not forced here.

These are the helper-level pins. The load-bearing, call-site-agnostic proof is
the connect-wiring test in ``test_seal_reconcile_reconnect_order.py``.
"""

from __future__ import annotations

from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnPhase
from sidequest.server.turn_status_roster import build_seal_reconcile_roster


def _snapshot_with_seats(seats: dict[str, str]) -> GameSnapshot:
    snapshot = GameSnapshot()
    snapshot.player_seats.update(seats)
    snapshot.turn_manager.phase = TurnPhase.InputCollection
    snapshot.turn_manager.player_count = len(seats)
    return snapshot


def _ids(roster: list) -> set[str]:
    return {e.player_id.as_str() for e in roster}


# ---------------------------------------------------------------------------
# AC1 (core) — first reconnector after a reload: only ONE live socket, but the
# durable roster knows the table is 2-seat. The reconcile must report BOTH
# seated PCs (denominator 2), so the first reconnector sees 0/2, not solo 0/1.
# ---------------------------------------------------------------------------


def test_reconcile_denominator_uses_durable_seats_not_live_sockets() -> None:
    """Both Adam and Eve are durably seated (``player_seats`` has 2). Only
    Adam's socket is live (the first reconnector after a reload), so the
    live-socket set passed in is ``["adam"]``. The reconcile roster must still
    carry BOTH peers — the durable seat roster, not the live socket, is the
    denominator. The pre-fix code builds from the live list and emits a solo
    roster (size 1 → 0/1), the exact measured bug."""
    snapshot = _snapshot_with_seats({"adam": "Adam", "eve": "Eve"})

    roster = build_seal_reconcile_roster(snapshot, ["adam"])

    assert _ids(roster) == {"adam", "eve"}, (
        "first reconnector must reconcile against the full durable seated-PC "
        "roster (Adam + Eve), not the single live socket — reporting 0/1 tells "
        "the table it is solo when Eve is durably seated and merely not yet "
        "reconnected"
    )
    assert len(roster) == 2, "denominator must be 2 (0/2), never the solo 0/1"


def test_reconcile_durable_denominator_with_sealed_peer_present() -> None:
    """Same 2-seat reload, but Adam's seal is still live in ``_submitted``
    (a same-process reconnect, not a full reload). The denominator must be the
    durable 2 AND Adam must read ``submitted`` — i.e. fixing the denominator
    must not drop the numerator when the seal IS still available."""
    snapshot = _snapshot_with_seats({"adam": "Adam", "eve": "Eve"})
    object.__getattribute__(snapshot.turn_manager, "_submitted").add("adam")

    roster = build_seal_reconcile_roster(snapshot, ["adam"])

    by_id = {e.player_id.as_str(): e.status for e in roster}
    assert by_id == {"adam": "submitted", "eve": "pending"}, (
        "denominator derives from durable seats (Adam+Eve) while the numerator "
        "still honors the live _submitted set — 1/2, not 0/1 and not 0/2"
    )


# ---------------------------------------------------------------------------
# Negative guard — a genuine solo session must STILL read 0/1. The fix must not
# manufacture phantom peers out of a single durable seat (context Assumptions:
# "Solo sessions genuinely report 0/1").
# ---------------------------------------------------------------------------


def test_reconcile_solo_session_stays_solo() -> None:
    """One durable seat, one live socket → roster size 1. The durable-seat fix
    must not invent a second row; solo is genuinely 0/1."""
    snapshot = _snapshot_with_seats({"adam": "Adam"})

    roster = build_seal_reconcile_roster(snapshot, ["adam"])

    assert _ids(roster) == {"adam"}, "solo session must report exactly its one seat"
    assert len(roster) == 1


# ---------------------------------------------------------------------------
# 45-2 phantom guard (helper level) — a mid-chargen peer has NO entry in the
# durable ``player_seats`` (it is written only on chargen commit). Deriving the
# denominator from ``player_seats`` therefore excludes the phantom for free.
# The load-bearing wiring proof of this lives in the reconnect-order wiring test
# (where the room actually holds a CHARGEN seat); this pin documents the
# invariant at the unit boundary.
# ---------------------------------------------------------------------------


def test_reconcile_excludes_peer_absent_from_durable_seats() -> None:
    """``player_seats`` holds only Adam (Eve is mid-chargen and uncommitted, so
    she is absent from the durable map). Even if Eve's player_id leaks into the
    live-socket list, the durable-seat denominator must exclude her — the table
    waits on committed PCs, never on someone still rolling a character (the
    entire reason ``playing_*`` was introduced in 45-2)."""
    snapshot = _snapshot_with_seats({"adam": "Adam"})

    roster = build_seal_reconcile_roster(snapshot, ["adam", "eve"])

    assert _ids(roster) == {"adam"}, (
        "a peer absent from the durable player_seats map (mid-chargen phantom) "
        "must never inflate the seal-reconcile denominator"
    )
