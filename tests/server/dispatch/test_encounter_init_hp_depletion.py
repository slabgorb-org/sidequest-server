import pytest
from pydantic import ValidationError

from sidequest.game.encounter import StructuredEncounter


def test_structured_encounter_defaults_win_condition_dial():
    enc = StructuredEncounter(
        encounter_type="x",
        player_metric={"name": "m", "current": 0, "starting": 0, "threshold": 7},
        opponent_metric={"name": "m", "current": 0, "starting": 0, "threshold": 7},
    )
    assert enc.win_condition == "dial_threshold"


def test_structured_encounter_accepts_hp_depletion():
    enc = StructuredEncounter(
        encounter_type="x",
        win_condition="hp_depletion",
        player_metric={"name": "hp", "current": 0, "starting": 0, "threshold": 1},
        opponent_metric={"name": "hp", "current": 0, "starting": 0, "threshold": 1},
    )
    assert enc.win_condition == "hp_depletion"


def test_structured_encounter_rejects_unknown_win_condition():
    # Literal type catches a typo at validation time (before Task 5 branches on it).
    with pytest.raises(ValidationError):
        StructuredEncounter(
            encounter_type="x",
            win_condition="hp_depltion",  # typo: missing 'e'
            player_metric={"name": "m", "current": 0, "starting": 0, "threshold": 7},
            opponent_metric={"name": "m", "current": 0, "starting": 0, "threshold": 7},
        )
