"""Wiring test for ADR-136 site 1 — the engagement-tick disposition beat.

Per server CLAUDE.md "No Source-Text Wiring Tests", the wiring assertion drives
the real ``_apply_npc_mentions`` handler over a synthetic snapshot and asserts on
the mutated ``Npc.disposition_log`` — it does NOT re-implement the call site by
hand. Removing the ``record_disposition_beat`` call in ``narration_apply.py``
(narration_apply.py ~1738) makes ``test_engagement_records_disposition_beat`` fail.
"""

from __future__ import annotations

from sidequest.game.disposition import Disposition
from sidequest.game.npc_development import ENGAGEMENT_BEAT_REASON
from sidequest.game.session import Npc
from tests.server.test_npc_development_pipeline import _core, _engage, _snapshot


def test_engagement_reason_constant():
    assert isinstance(ENGAGEMENT_BEAT_REASON, str) and ENGAGEMENT_BEAT_REASON


def test_engagement_records_disposition_beat():
    # Drive the REAL production handler: one npcs_hit engagement of a fresh Npc.
    location = "Parlor"
    snap = _snapshot(Npc(core=_core("Boris"), disposition=Disposition(0)), location=location)
    before = int(snap.npcs[0].disposition)

    _engage(snap, "Boris", turn=7)

    npc = snap.npcs[0]
    # The disposition actually moved (a beat means the standing moved).
    assert int(npc.disposition) > before
    # The beat was persisted via the seam at the real call site.
    assert npc.disposition_log, "engagement tick recorded no disposition beat"
    beat = npc.disposition_log[-1]
    assert beat.reason == ENGAGEMENT_BEAT_REASON
    assert beat.turn == 7
    assert beat.location == location
    assert beat.delta == int(npc.disposition) - before
