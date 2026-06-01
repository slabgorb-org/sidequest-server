"""Wiring test for ADR-136 site 1 — the engagement-tick disposition beat.

Per server CLAUDE.md "No Source-Text Wiring Tests", the wiring assertion drives
the real ``_apply_npc_mentions`` handler over a synthetic snapshot and asserts on
the mutated ``Npc.disposition_log`` — it does NOT re-implement the call site by
hand. Removing the ``record_disposition_beat`` call in ``narration_apply.py``
(narration_apply.py ~1738) makes ``test_engagement_records_disposition_beat`` fail.
"""

from __future__ import annotations

import inspect

from sidequest.agents.tools import update_npc_disposition as tool_mod
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


def test_tool_records_beat_from_args_reason():
    """Site 2 smoke: the update_npc_disposition handler still resolves (args, ctx).

    Per server CLAUDE.md "No Source-Text Wiring Tests", we do NOT grep the handler
    body here. This is a cheap smoke that the site-2 edit didn't break arg handling;
    the real end-to-end wiring proof for this site is the OTEL-driven dispatch test
    in Task 11. The ``@tool`` decorator returns the bare coroutine (it ``return fn``s
    after registering), so the handler IS ``update_npc_disposition`` — there is no
    ``.fn`` wrapper attribute.
    """
    sig = inspect.signature(tool_mod.update_npc_disposition)
    assert "args" in sig.parameters and "ctx" in sig.parameters
