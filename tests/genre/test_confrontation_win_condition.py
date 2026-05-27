import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import ConfrontationDef, WinCondition

BEAT = {"id": "shoot", "label": "Shoot", "kind": "strike", "stat_check": "Physique"}
METRIC = {"name": "momentum", "starting": 0, "threshold": 7}
# Combat hp_depletion confrontations now require opponent_default_stats with
# the reserved hp/armor_class keys at load time (Task 9). These tests are
# about metric-optionality, not opponent stats — supply a valid stat block so
# the validator passes; the metric assertions below are unchanged.
OPP_STATS = {"hp": 12, "armor_class": 13, "dexterity": 12}


def test_default_win_condition_is_dial_threshold():
    c = ConfrontationDef(
        type="combat",
        label="Firefight",
        category="combat",
        player_metric=METRIC,
        opponent_metric=METRIC,
        beats=[BEAT],
    )
    assert c.win_condition == WinCondition.dial_threshold


def test_hp_depletion_allows_missing_metrics():
    c = ConfrontationDef(
        type="combat",
        label="Firefight",
        category="combat",
        win_condition="hp_depletion",
        opponent_default_stats=OPP_STATS,
        beats=[BEAT],
    )
    assert c.win_condition == WinCondition.hp_depletion
    assert c.player_metric is None and c.opponent_metric is None


def test_dial_threshold_without_metrics_fails_loud():
    with pytest.raises(ValidationError, match="player_metric"):
        ConfrontationDef(type="combat", label="Firefight", category="combat", beats=[BEAT])


def test_hp_depletion_with_metrics_is_allowed():
    c = ConfrontationDef(
        type="combat",
        label="Firefight",
        category="combat",
        win_condition="hp_depletion",
        opponent_default_stats=OPP_STATS,
        player_metric=METRIC,
        opponent_metric=METRIC,
        beats=[BEAT],
    )
    assert c.win_condition == WinCondition.hp_depletion
    assert c.player_metric is not None
