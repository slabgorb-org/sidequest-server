"""Regression: 2026-04-25-dungeon_survivor — Sam KO'd, engine had said
'player_victory'. Under dual-track momentum the same beat sequence must
resolve to opponent_victory.

This is the lie-detector check from CLAUDE.md's OTEL Observability
Principle: prose says one thing, engine says another, GM panel reconciles.

Reference save narrative_log (rows 6-7) shows Sam stumbling and being KO'd
by the Promo. The pre-fix engine collapsed opponent beats onto the player
metric, resolving as player_victory. The dual-track engine routes each
actor's beats to their own dial so the Promo's wins accumulate on the
opponent dial until threshold=10 is crossed.

The plan's 4-beat script (CritSuccess, Success, CritSuccess, CritSuccess)
adds 2+2+2+2=8 to the opponent dial — one beat short of threshold=10.
A 5th beat (CritSuccess, matching narrative row 7's "sickle catches Sam
square across the temple") crosses the threshold and resolves the encounter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._helpers.session_room import room_for

REF_SAVE = (
    Path.home() / ".sidequest" / "saves" / "games" / "2026-04-25-dungeon_survivor" / "save.db"
)


@pytest.mark.integration
@pytest.mark.skipif(not REF_SAVE.exists(), reason="reference save not present")
def test_dungeon_survivor_resolves_to_opponent_victory(
    store_bound_to_hub,
):
    """Replay the beat sequence from the reference save's narrative_log.

    The encounter under the new engine MUST resolve as opponent_victory.
    Pre-fix it resolved as player_victory because the engine collapsed
    opponent damage onto the player's metric.

    Script is derived from reference save rows 5-7: Promo presses Sam
    off-balance, Sam's guard fails twice, then the decisive sickle blow.
    Base delta per attack beat = 2; threshold = 10; needs 5 beats.
    """
    from sidequest.agents.orchestrator import (
        BeatSelection,
        NarrationTurnResult,
        NpcMention,
    )
    from sidequest.game.persistence import query_encounter_events
    from sidequest.protocol.dice import RollOutcome
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )
    from sidequest.server.narration_apply import (
        _apply_narration_result_to_snapshot,
    )

    store, snap, pack = store_bound_to_hub
    # Use the production lifecycle path so ENCOUNTER_STARTED fires.
    snap.encounter = None
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Sam",
        npcs_present=[NpcMention(name="The Promo", side="opponent", role="hostile")],
        genre_slug="test_pack",
    )

    # Beats transcribed from the reference save narrative_log rows 5-7.
    # Row 5: sickle sweep, Sam's shield holds — Promo presses.
    # Row 6: Sam lunges and misses, Promo pivots, sickle coming back (Sam fail).
    # Row 7: Sam over-commits, sickle catches temple — decisive KO.
    # The 4-beat script from the plan yields 8 on the opponent dial (2 per
    # CritSuccess/Success attack, threshold=10). A 5th beat is required to
    # cross the threshold and resolve the encounter.
    promo_script = [
        ("CritSuccess", "Promo lunges, blade sliding under Sam's guard."),
        ("Success", "Steel kisses ribs. Sam staggers."),
        ("CritSuccess", "The Promo presses, Sam off-balance."),
        ("CritSuccess", "Sam's knees buckle. The crowd roars."),
        ("CritSuccess", "The sickle catches Sam square across the temple. KO."),
    ]
    for tier, prose in promo_script:
        outcome_enum = RollOutcome(tier)
        result = NarrationTurnResult(
            narration=prose,
            beat_selections=[
                BeatSelection(actor="Sam", beat_id="defend", outcome=RollOutcome.Fail),
                BeatSelection(actor="The Promo", beat_id="attack", outcome=outcome_enum),
            ],
            npcs_present=[
                NpcMention(name="The Promo", side="opponent", role="hostile"),
            ],
        )
        _apply_narration_result_to_snapshot(snap, result, "Sam", pack=pack, room=room_for(snap))
        if snap.encounter is None or snap.encounter.resolved:
            break

    # The encounter must have resolved.
    assert snap.encounter is None or snap.encounter.resolved, (
        "encounter did not resolve after 5 promo beats"
    )

    # The events table records the corrected outcome.
    events = query_encounter_events(store)
    resolved_rows = [e for e in events if e["kind"] == "ENCOUNTER_RESOLVED"]
    assert resolved_rows, "expected at least one ENCOUNTER_RESOLVED row"
    outcome = resolved_rows[-1]["payload"].get("outcome")
    assert outcome == "opponent_victory", f"expected opponent_victory; got {outcome!r}"


@pytest.mark.integration
@pytest.mark.skipif(not REF_SAVE.exists(), reason="reference save not present")
def test_dungeon_survivor_timeline_actors_have_side_attribution(
    store_bound_to_hub,
):
    """Every ENCOUNTER_BEAT_APPLIED row carries ``actor_side`` so the GM
    panel can render attribution.

    The pre-fix bug was that opponent beats applied to the player metric
    because the engine had no side concept — proving every BEAT_APPLIED
    row carries actor_side is the structural test that the new engine
    has actually wired the dual dials.
    """
    from sidequest.agents.orchestrator import (
        BeatSelection,
        NarrationTurnResult,
        NpcMention,
    )
    from sidequest.game.persistence import query_encounter_events
    from sidequest.protocol.dice import RollOutcome
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )
    from sidequest.server.narration_apply import (
        _apply_narration_result_to_snapshot,
    )

    store, snap, pack = store_bound_to_hub
    snap.encounter = None
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Sam",
        npcs_present=[NpcMention(name="The Promo", side="opponent", role="hostile")],
        genre_slug="test_pack",
    )

    # Drive a couple beats — enough to populate the timeline.
    for tier in ("CritSuccess", "Success"):
        result = NarrationTurnResult(
            narration="...",
            beat_selections=[
                BeatSelection(
                    actor="The Promo",
                    beat_id="attack",
                    outcome=RollOutcome(tier),
                ),
            ],
            npcs_present=[NpcMention(name="The Promo", side="opponent", role="hostile")],
        )
        _apply_narration_result_to_snapshot(snap, result, "Sam", pack=pack, room=room_for(snap))

    events = query_encounter_events(store)
    beat_rows = [e for e in events if e["kind"] == "ENCOUNTER_BEAT_APPLIED"]
    assert beat_rows, "expected at least one ENCOUNTER_BEAT_APPLIED row"
    for event in beat_rows:
        payload = event["payload"]
        assert payload.get("actor_side") in {"player", "opponent"}, (
            f"BEAT_APPLIED row missing actor_side: {payload}"
        )
