"""Story 73-2 (re-authored 2026-06-17, Fate Contest binding): the ``trial``
confrontation (tea_and_murder) must offer a voluntary withdraw/concede exit — the
same soft-lock fix 73-1 applied to negotiation/scandal and 59-8 applied to
social_duel, now for the courtroom. Like its three siblings, ``trial`` is now a
**Fate Contest** (``resolution_mode: contest``, ADR-144), so the original
``opposed_check`` content test was deleted with the conversion.

Two playtest-grounded failures motivate the surviving tests (67-10, 2026-05-31,
Glenross):

  1. A committed "resolution beat" (Concede Gracefully) did NOT flip
     ``encounter.resolved`` — no ``encounter.resolved`` span fired, the panel
     never tore down, and the action input stayed locked (soft-lock).
  2. ``trial`` had NO terminal resolution beat at all — only ``yield``
     (``kind: angle``), which advances the opponent, it does not end the trial.

The resolution beat is discovered structurally (``b.resolution is True``) rather
than by a hard-coded id, so Dev is free to name the courtroom withdraw beat
(``rest_case`` / ``withdraw_charge`` / ``concede`` — author's choice) without
breaking these tests. NB: ``trial`` must NOT borrow ``auction``'s ``withdraw``
beat id semantics — auction's ``withdraw`` is a table_resolution beat with no
``resolution: true`` flag.

The voluntary-concede OTEL span (AC-3) is driven as a committed PLAYER
beat-selection: the narrator-beat path in ``narration_apply`` fires
``encounter_resolved_span(source="narrator_beat")`` regardless of resolution_mode,
so the concede resolves the same way whether the contested beats are dial, opposed
or contest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.beat_kinds import apply_beat
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.telemetry.spans.encounter import SPAN_ENCOUNTER_RESOLVED
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = (
    Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
)

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)

# Every outcome tier a d20 can land — INCLUDING CritSuccess, the one tier with
# distinct push semantics (DEFAULT_DELTAS grants a "Clean Exit" fleeting tag on
# a CritSuccess push). A voluntary withdraw must resolve on ALL of them.
_ALL_TIERS = [
    RollOutcome.Fail,
    RollOutcome.CritFail,
    RollOutcome.Tie,
    RollOutcome.Success,
    RollOutcome.CritSuccess,
]
# The four social confrontations that must each offer a voluntary exit (AC-3).
_SOCIAL_CONFRONTATIONS = ["trial", "social_duel", "negotiation", "scandal"]


def _pack():
    return load_genre_pack(CONTENT_GENRE_PACKS / "tea_and_murder")


def _cdef(ctype: str):
    cdef = find_confrontation_def(_pack().rules.confrontations, ctype)
    assert cdef is not None, f"tea_and_murder must define a {ctype} confrontation"
    return cdef


def _resolution_beat(ctype: str):
    """The voluntary-exit beat: any beat the author flagged ``resolution: true``.

    Discovered structurally so the test does not couple to the beat's id.
    """
    cdef = _cdef(ctype)
    beat = next((b for b in cdef.beats if b.resolution is True), None)
    assert beat is not None, (
        f"{ctype} must define a voluntary withdraw/concede beat carrying "
        f"resolution: true (a declarative resolver) — none found among "
        f"{[b.id for b in cdef.beats]}"
    )
    return beat


def _trial_encounter() -> StructuredEncounter:
    """A deadlocked trial — both conviction dials at 0, both sides seated. The
    voluntary withdraw must break this deadlock the way the playtest never could.

    Thresholds are 7 to match the shipped opposed_check calibration (ADR-093,
    enforced by tests/genre/test_confrontation_calibration.py)."""
    return StructuredEncounter(
        encounter_type="trial",
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name="conviction", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="conviction", current=0, starting=0, threshold=7),
        actors=[
            EncounterActor(name="Inspector Pryce", role="participant", side="player"),
            EncounterActor(name="Crown Prosecutor", role="participant", side="opponent"),
        ],
    )


# ── AC-1: trial has an authored voluntary-exit resolution beat ───────────────


def test_trial_has_withdraw_resolution_beat():
    """AC-1: trial offers an author-committed withdraw/concede beat that carries
    ``resolution: true``. RED: trial's only non-strike beat is ``yield``
    (``kind: angle``), which advances the opponent rather than ending the trial."""
    beat = _resolution_beat("trial")
    assert beat.resolution is True


# ── AC-3: a committed resolution beat ends the trial on ANY outcome tier ──────


@pytest.mark.parametrize("outcome", _ALL_TIERS)
def test_trial_withdraw_resolves_on_every_outcome_tier(outcome: RollOutcome):
    """AC-3 (+ compounds with 73-4): a voluntary withdraw ends the trial
    regardless of the d20 result. A concede that only resolves on Success is the
    exact soft-lock from the playtest — a Fail/CritFail/Tie concede must still
    flip ``encounter.resolved``."""
    enc = _trial_encounter()
    beat = _resolution_beat("trial")
    result = apply_beat(enc, enc.actors[0], beat, outcome, turn=1)
    assert result.resolved is True, f"trial withdraw must resolve on {outcome}"
    assert enc.resolved is True
    assert enc.outcome == f"resolution_beat:{beat.id}"


@pytest.mark.parametrize("ctype", _SOCIAL_CONFRONTATIONS)
def test_every_social_confrontation_offers_a_voluntary_exit(ctype: str):
    """AC-3 (cross-confrontation invariant): trial, social_duel, negotiation, and
    scandal must each expose a ``resolution: true`` beat so no player is ever
    soft-locked. RED: only ``trial`` is missing one — the other three were fixed
    by 73-1 / 59-8 and this guards against regressing them.

    `_resolution_beat` raises if no `resolution: true` beat exists (the soft-lock
    condition). The assertion below is NOT a re-check of that flag — it pins the
    *shape* of the resolver: a voluntary exit is a ``push`` beat (the declarative
    forfeit pattern shared by withdraw_case/concede/walk_away/weather_it), not an
    incidental angle/strike beat that happened to carry the flag."""
    beat = _resolution_beat(ctype)
    assert beat.kind == "push", (
        f"{ctype}'s voluntary-exit beat {beat.id!r} should be a push-kind forfeit, "
        f"got kind={beat.kind!r}"
    )


# ── AC-4: a voluntary withdraw is neutral, not a punitive defeat ──────────────


def test_trial_withdraw_outcome_is_neutral_resolution_not_a_victory():
    """AC-4: the loser's voluntary exit is recorded as a resolution_beat outcome,
    NOT ``opponent_victory`` — so downstream lethality/harm (which keys on a
    combat victory) is absorbed by the choice, not applied punitively. The player
    withdraws while BEHIND on the dials (0 vs opponent ahead) and still exits
    cleanly."""
    enc = _trial_encounter()
    enc.opponent_metric.current = 6  # opponent is winning; player concedes anyway
    beat = _resolution_beat("trial")
    result = apply_beat(enc, enc.actors[0], beat, RollOutcome.Fail, turn=3)
    assert result.resolved is True
    # The exact-string pin IS the not-a-victory guarantee: an outcome of
    # opponent_victory/player_victory would fail this equality. (No separate
    # `not in (victory tuple)` check — it would be vacuous once this passes.)
    assert enc.outcome == f"resolution_beat:{beat.id}"
    assert enc.structured_phase == EncounterPhase.Resolution


# ── AC-3 (OTEL): conceding through the real opposed branch fires the span ─────


def test_trial_concede_emits_encounter_resolved_span(otel_capture):
    """AC-3 (lie-detector): committing the trial withdraw beat through the real
    narration-apply path flips ``encounter.resolved`` AND fires the
    ``encounter.resolved`` OTEL span. The ABSENCE of that span was the playtest
    signal that the panel never tore down.

    Driven as a player beat-selection (the authored resolution beat) — the
    narrator-beat path fires encounter_resolved_span(source="narrator_beat")
    regardless of resolution_mode (contest or otherwise). No engine change needed.
    """
    beat = _resolution_beat("trial")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.encounter = StructuredEncounter(
        encounter_type="trial",
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name="conviction", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="conviction", current=0, starting=0, threshold=7),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(
                name="Inspector Pryce",
                role="participant",
                side="player",
                per_actor_state={"stats": {"Cunning": 12, "Passion": 12, "Nerve": 12}},
            ),
            EncounterActor(
                name="Crown Prosecutor",
                role="participant",
                side="opponent",
                per_actor_state={"stats": {"Cunning": 12, "Passion": 12, "Nerve": 12}},
            ),
        ],
    )

    # Drive the concede as a committed PLAYER beat-selection — put the resolution
    # beat in result.beat_selections as the player's beat. The narrator-beat path
    # fires encounter_resolved_span(source="narrator_beat") regardless of
    # resolution_mode, so no engine change is needed for this test.
    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(
                actor="Inspector Pryce",
                beat_id=beat.id,
                outcome=RollOutcome.Fail,
            ),
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Inspector Pryce",
        pack=_pack(),
        from_explicit_action=True,
        room=room_for(snap),
    )

    trial_resolved_spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_ENCOUNTER_RESOLVED
        and (s.attributes or {}).get("encounter_type") == "trial"
    ]
    assert trial_resolved_spans, (
        "conceding the trial must fire the encounter.resolved lie-detector span FOR "
        "the trial encounter — its absence is the soft-lock signature from the "
        "67-10 playtest"
    )
    assert snap.encounter is not None, "the trial encounter must not be dropped from state"
    assert snap.encounter.resolved is True, (
        "after a committed concede the confrontation must be resolved so the panel "
        "tears down and the input unlocks"
    )
    assert snap.encounter.outcome == f"resolution_beat:{beat.id}", (
        "the concede must record a voluntary resolution_beat outcome, not a "
        f"victory/defeat; got {snap.encounter.outcome!r}"
    )
