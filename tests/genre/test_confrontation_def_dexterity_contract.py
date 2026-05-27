"""SWN P4: a combat hp_depletion confrontation must author opponent `dexterity`
at LOAD time (third reserved combat key), not discover it missing mid-seating."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import (
    OPPONENT_RESERVED_STAT_KEYS,
    ConfrontationDef,
    WinCondition,
)


def _combat_kwargs(**overrides):
    base = dict(
        confrontation_type="firefight",
        label="Firefight",
        category="combat",
        win_condition=WinCondition.hp_depletion,
        opponent_default_stats={"hp": 7, "armor_class": 12, "dexterity": 13},
        beats=[
            {
                "id": "shoot",
                "label": "Shoot",
                "stat_check": "Physique",
                "base": 1,
                "kind": "strike",
            }
        ],
    )
    base.update(overrides)
    return base


def test_dexterity_is_a_reserved_combat_key():
    assert "dexterity" in OPPONENT_RESERVED_STAT_KEYS


def test_combat_hp_depletion_requires_opponent_dexterity():
    with pytest.raises(ValidationError, match="dexterity"):
        ConfrontationDef(**_combat_kwargs(opponent_default_stats={"hp": 7, "armor_class": 12}))


def test_opponent_dexterity_must_be_at_least_three():
    with pytest.raises(ValidationError, match="dexterity"):
        ConfrontationDef(
            **_combat_kwargs(opponent_default_stats={"hp": 7, "armor_class": 12, "dexterity": 2})
        )


def test_valid_combat_confrontation_exposes_opponent_dexterity():
    cdef = ConfrontationDef(**_combat_kwargs())
    assert cdef.opponent_dexterity == 13


def test_opponent_ability_scores_strips_dexterity():
    cdef = ConfrontationDef(**_combat_kwargs(
        opponent_default_stats={"hp": 7, "armor_class": 12, "dexterity": 13, "Physique": 11}
    ))
    scores = cdef.opponent_ability_scores()
    assert "dexterity" not in scores and "hp" not in scores and "armor_class" not in scores
    assert scores == {"Physique": 11}
