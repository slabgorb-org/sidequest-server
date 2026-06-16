"""End-to-end wiring: narration_apply → encounter resolution → events table.

Asserts:
1. encounter_dispatch_helper.run_to_resolution drives the live engine to
   opponent_victory.
2. The SQLite events table records the full encounter timeline in order:
   ENCOUNTER_STARTED → ENCOUNTER_BEAT_APPLIED (multiple) →
   ENCOUNTER_METRIC_ADVANCE (multiple) → ENCOUNTER_RESOLVED.
3. snapshot.pending_resolution_signal is set with the correct outcome.

Per CLAUDE.md "Every Test Suite Needs a Wiring Test". This is the round-trip
proof that all Phase 1+2 plumbing actually fires from production code paths.

Note on ENCOUNTER_STARTED: store_bound_to_hub pre-builds a StructuredEncounter
directly without going through instantiate_encounter_from_trigger, so the
lifecycle event does not fire from the fixture. We clear snap.encounter and
call instantiate_encounter_from_trigger directly here so the full event
sequence (including STARTED) is exercised from production code.

Note on [ENCOUNTER RESOLVED] zone: tests/agents/test_narrator_prompt.py already
asserts that a TurnContext with pending_resolution_signal renders
"[ENCOUNTER RESOLVED]" and "outcome: opponent_victory" in the narrator prompt.
That coverage is sufficient; we do not duplicate it here.
"""

from __future__ import annotations

import pytest

from sidequest.agents.orchestrator import NpcMention
from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger


@pytest.mark.integration
def test_full_encounter_round_trip_records_timeline_and_sets_signal(
    store_bound_to_hub,
    encounter_dispatch_helper,
):
    store, snap, pack = store_bound_to_hub

    # store_bound_to_hub pre-builds a StructuredEncounter without going through
    # instantiate_encounter_from_trigger, so ENCOUNTER_STARTED would not fire.
    # Clear it and use the lifecycle function so every event in the sequence
    # is emitted from production code paths.
    snap.encounter = None
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Sam",
        npcs_present=[NpcMention(name="Promo", side="opponent", role="hostile")],
        genre_slug="test_pack",
    )

    # Drive opponent to victory through the real narration_apply path.
    encounter_dispatch_helper.run_to_resolution(snap, pack, winner="opponent")

    # --- Engine state ---
    assert snap.encounter is not None
    assert snap.encounter.resolved is True
    assert snap.pending_resolution_signal is not None
    assert snap.pending_resolution_signal.outcome == "opponent_victory"

    # --- Events table ---
    rows = list(
        store._conn.execute(
            "SELECT kind FROM events WHERE kind LIKE 'ENCOUNTER_%' ORDER BY seq"
        ).fetchall()
    )
    kinds = [r[0] for r in rows]

    assert "ENCOUNTER_STARTED" in kinds, f"ENCOUNTER_STARTED missing; got {kinds!r}"
    assert "ENCOUNTER_BEAT_APPLIED" in kinds, f"ENCOUNTER_BEAT_APPLIED missing; got {kinds!r}"
    assert "ENCOUNTER_METRIC_ADVANCE" in kinds, f"ENCOUNTER_METRIC_ADVANCE missing; got {kinds!r}"
    assert kinds[-1] == "ENCOUNTER_RESOLVED", f"last row must be ENCOUNTER_RESOLVED; got {kinds!r}"
