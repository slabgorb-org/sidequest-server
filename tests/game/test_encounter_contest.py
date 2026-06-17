from sidequest.game.encounter import ContestState, EncounterMetric, StructuredEncounter
from sidequest.genre.models.rules import ResolutionMode

_METRIC = EncounterMetric(name="victory", current=0, starting=0, threshold=3)


def test_resolution_mode_has_contest():
    assert ResolutionMode.contest == "contest"


def test_encounter_carries_optional_contest_state():
    enc = StructuredEncounter(
        encounter_type="negotiation",
        category="social",
        actors=[],
        player_metric=_METRIC,
        opponent_metric=_METRIC,
    )
    assert enc.contest is None
    enc.contest = ContestState(target=3)
    assert (enc.contest.player_victories, enc.contest.opponent_victories) == (0, 0)
    assert enc.contest.target == 3
