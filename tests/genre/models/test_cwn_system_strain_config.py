from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import CwnConfig, RulesConfig, SystemStrainConfig

_FLAVOR = ["Brawn", "Reflex", "Body", "Tech", "Instinct", "Cool"]
_AMAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}


def test_system_strain_config_defaults():
    cfg = SystemStrainConfig()
    assert cfg.max_source == "CONSTITUTION"
    assert cfg.rest_recovery_per_night == 1
    assert cfg.first_aid_cost == 1


def test_cwn_config_has_system_strain_by_default():
    cfg = CwnConfig(attribute_map=_AMAP)
    assert isinstance(cfg.system_strain, SystemStrainConfig)
    assert cfg.system_strain.max_source == "CONSTITUTION"


def test_cwn_rejects_max_source_not_in_attribute_map():
    bad = CwnConfig(attribute_map=_AMAP, system_strain=SystemStrainConfig(max_source="LUCK"))
    with pytest.raises(ValidationError, match="max_source"):
        RulesConfig(ruleset="cwn", ability_score_names=_FLAVOR, cwn=bad)


def test_cwn_accepts_valid_max_source():
    rules = RulesConfig(
        ruleset="cwn",
        ability_score_names=_FLAVOR,
        cwn=CwnConfig(
            attribute_map=_AMAP, system_strain=SystemStrainConfig(max_source="CONSTITUTION")
        ),
    )
    assert rules.cwn is not None
    assert rules.cwn.system_strain.max_source == "CONSTITUTION"
