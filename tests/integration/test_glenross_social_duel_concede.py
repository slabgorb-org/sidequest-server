"""Regression: the tea_and_murder ``social_duel`` concede beat must ALWAYS
resolve the duel — even on a failed d20 roll.

Playtest 59-8 (Glenross) BLOCKING: a player who clicked "Concede Gracefully"
and rolled a Fail stayed soft-locked in a Duel of Wits the fiction had already
closed. The concede beat is ``kind: push``, and a ``push`` only yields
``resolution`` on a Success tier (``DEFAULT_DELTAS`` in ``beat_kinds.py``), so a
failed concession left ``apply_result.resolved=False`` → no ENCOUNTER_RESOLVED →
the confrontation panel never tore down.

The fix is the declarative ``resolution: true`` flag on the concede beat
(BeatDef.resolution, honored at ``apply_beat`` regardless of outcome tier). This
test loads the REAL pack so it pins both the content invariant (the flag is
authored) and the engine wiring (apply_beat honors it on a failing tier).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.confrontation import find_confrontation_def

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)


def _concede_beat():
    pack = load_genre_pack(CONTENT_GENRE_PACKS / "tea_and_murder")
    cdef = find_confrontation_def(pack.rules.confrontations, "social_duel")
    assert cdef is not None, "tea_and_murder must define a social_duel confrontation"
    concede = next((b for b in cdef.beats if b.id == "concede"), None)
    assert concede is not None, "social_duel must define a 'concede' beat"
    return concede


def _social_duel_encounter() -> StructuredEncounter:
    # Mirrors the live shape: dual 5/5 dials, both sides seated, dials at 0
    # (neither side landed a barb) — the deadlocked state from the playtest.
    return StructuredEncounter(
        encounter_type="social_duel",
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name="barbs_landed", current=0, starting=0, threshold=5),
        opponent_metric=EncounterMetric(name="barbs_landed", current=0, starting=0, threshold=5),
        actors=[
            EncounterActor(name="Inspector Pryce", role="participant", side="player"),
            EncounterActor(name="Sir Iain Ross", role="participant", side="opponent"),
        ],
    )


def test_concede_beat_carries_resolution_flag():
    """Content invariant: the authored concede beat is a declarative resolver."""
    assert _concede_beat().resolution is True


@pytest.mark.parametrize(
    "outcome",
    [RollOutcome.Fail, RollOutcome.CritFail, RollOutcome.Tie, RollOutcome.Success],
)
def test_concede_resolves_on_every_outcome_tier(outcome: RollOutcome):
    """A voluntary forfeit ends the duel regardless of the d20 result.

    Pre-fix this passed only for Success (push Success → resolution); Fail /
    CritFail / Tie left ``resolved=False`` and soft-locked the player.
    """
    enc = _social_duel_encounter()
    result = apply_beat(enc, enc.actors[0], _concede_beat(), outcome, turn=1)
    assert result.resolved is True, f"concede must resolve on {outcome}"
    assert enc.resolved is True
    assert enc.outcome == "resolution_beat:concede"
