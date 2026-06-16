import pytest

from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    ResolutionMode,
    WinCondition,
)


def _table_cdef(**kw) -> ConfrontationDef:
    base = dict(
        type="poker",
        label="Poker",
        category="social",
        resolution_mode=ResolutionMode.table_resolution,
        win_condition=WinCondition.table_showdown,
        table_game="poker",
        max_decision_points=3,
        beats=[BeatDef(id="fold", label="Fold", kind="push", stat_check="WIS", base=0)],
    )
    base.update(kw)
    return ConfrontationDef(**base)


def test_table_resolution_mode_exists():
    assert ResolutionMode("table_resolution") is ResolutionMode.table_resolution


def test_table_showdown_win_condition_exists():
    assert WinCondition("table_showdown") is WinCondition.table_showdown


def test_table_confrontation_does_not_require_dials():
    # table_showdown must NOT trip the dial_threshold metric requirement
    cdef = _table_cdef()
    assert cdef.player_metric is None
    assert cdef.opponent_metric is None
    assert cdef.table_game == "poker"
    assert cdef.max_decision_points == 3


def test_table_resolution_requires_table_game():
    with pytest.raises(ValueError, match="table_game"):
        _table_cdef(table_game=None)


def test_table_resolution_requires_positive_decision_points():
    with pytest.raises(ValueError, match="max_decision_points"):
        _table_cdef(max_decision_points=0)


def test_table_resolution_requires_table_showdown_win_condition():
    with pytest.raises(ValueError, match="win_condition"):
        _table_cdef(win_condition=WinCondition.dial_threshold)


def test_non_table_confrontation_leaves_table_game_none():
    cdef = ConfrontationDef(
        type="brawl",
        label="Brawl",
        category="combat",
        win_condition=WinCondition.hp_depletion,
        opponent_default_stats={"hp": 8, "armor_class": 12, "dexterity": 10},
        beats=[BeatDef(id="strike", label="Strike", kind="strike", stat_check="STR", base=1)],
    )
    assert cdef.table_game is None
