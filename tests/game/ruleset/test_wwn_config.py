from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import RulesConfig, SystemStrainConfig, WwnConfig

_EH_FLAVOR = ["Strength", "Agility", "Endurance", "Insight", "Spirit", "Harmony"]
_EH_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}


def test_wwn_config_inherits_swn_defaults():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    assert cfg.save_base == 15
    assert cfg.unarmored_ac == 10
    assert cfg.difficulties["formidable"] == 14
    assert cfg.attribute_map["WISDOM"] == "Spirit"


def test_wwn_config_has_strain_and_trauma_defaults():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    assert cfg.system_strain.max_source == "CONSTITUTION"
    assert cfg.trauma.default_trauma_target == 6


def test_wwn_config_has_no_hacking_field():
    # WWN has no cyberspace; the field must not exist (extra='forbid' rejects it).
    with pytest.raises(ValidationError, match="hacking"):
        WwnConfig(attribute_map=_EH_AMAP, hacking={"default_tier": "x", "security_tiers": {"x": 7}})


def test_rules_wwn_requires_complete_attribute_map():
    with pytest.raises(ValidationError, match="attribute_map"):
        RulesConfig(
            ruleset="wwn",
            ability_score_names=_EH_FLAVOR,
            wwn=WwnConfig(attribute_map={"STRENGTH": "Strength"}),  # missing 5 keys
        )


def test_rules_wwn_rejects_flavor_not_in_ability_scores():
    bad = dict(_EH_AMAP, CHARISMA="Charm")  # Charm not declared
    with pytest.raises(ValidationError, match="ability_score_names"):
        RulesConfig(ruleset="wwn", ability_score_names=_EH_FLAVOR, wwn=WwnConfig(attribute_map=bad))


def test_rules_wwn_accepts_complete_map():
    rules = RulesConfig(
        ruleset="wwn", ability_score_names=_EH_FLAVOR, wwn=WwnConfig(attribute_map=_EH_AMAP)
    )
    assert rules.wwn is not None
    assert rules.wwn.attribute_map["INTELLIGENCE"] == "Insight"
    assert rules.ruleset_config() is rules.wwn


def test_rules_wwn_with_no_config_block_fails_loud():
    with pytest.raises(ValidationError, match="attribute_map"):
        RulesConfig(ruleset="wwn", ability_score_names=_EH_FLAVOR)


def test_rules_wwn_strain_source_must_be_in_map():
    cfg = WwnConfig(
        attribute_map=_EH_AMAP, system_strain=SystemStrainConfig(max_source="NONEXISTENT")
    )
    with pytest.raises(ValidationError, match="max_source"):
        RulesConfig(ruleset="wwn", ability_score_names=_EH_FLAVOR, wwn=cfg)
