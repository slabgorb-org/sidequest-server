from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import CwnConfig, RulesConfig, TraumaConfig

_FLAVOR = ["Brawn", "Reflex", "Body", "Tech", "Instinct", "Cool"]
_AMAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}


def test_trauma_config_defaults():
    cfg = TraumaConfig()
    assert cfg.default_trauma_target == 6
    assert cfg.mortal_injury_rounds == 6
    assert cfg.major_injury_save == "physical"


def test_cwn_config_has_trauma_by_default():
    cfg = CwnConfig(attribute_map=_AMAP)
    assert isinstance(cfg.trauma, TraumaConfig)
    assert cfg.trauma.default_trauma_target == 6


def test_cwn_accepts_custom_trauma():
    rules = RulesConfig(
        ruleset="cwn",
        ability_score_names=_FLAVOR,
        cwn=CwnConfig(attribute_map=_AMAP, trauma=TraumaConfig(default_trauma_target=7)),
    )
    assert rules.cwn is not None
    assert rules.cwn.trauma.default_trauma_target == 7


def test_invalid_major_injury_save_rejected():
    with pytest.raises(ValidationError):
        RulesConfig(
            ruleset="cwn",
            ability_score_names=_FLAVOR,
            cwn=CwnConfig(attribute_map=_AMAP, trauma=TraumaConfig(major_injury_save="fortitude")),
        )
