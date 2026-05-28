from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import CwnConfig, RulesConfig

_NEON_FLAVOR = ["Brawn", "Reflex", "Body", "Tech", "Instinct", "Cool"]
_NEON_AMAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}


def test_cwn_config_inherits_swn_defaults():
    cfg = CwnConfig(attribute_map=_NEON_AMAP)
    # CWN's "16 - level" save target equals SWN's "save_base - (level-1)" with save_base=15.
    assert cfg.save_base == 15
    assert cfg.unarmored_ac == 10
    assert cfg.difficulties["formidable"] == 14
    assert cfg.attribute_map["CONSTITUTION"] == "Body"


def test_rules_cwn_requires_complete_attribute_map():
    with pytest.raises(ValidationError, match="attribute_map"):
        RulesConfig(
            ruleset="cwn",
            ability_score_names=_NEON_FLAVOR,
            cwn=CwnConfig(attribute_map={"STRENGTH": "Brawn"}),  # missing 5 keys
        )


def test_rules_cwn_rejects_flavor_not_in_ability_scores():
    bad = dict(_NEON_AMAP, CHARISMA="Swagger")  # Swagger not declared
    with pytest.raises(ValidationError, match="ability_score_names"):
        RulesConfig(
            ruleset="cwn",
            ability_score_names=_NEON_FLAVOR,
            cwn=CwnConfig(attribute_map=bad),
        )


def test_rules_cwn_accepts_complete_map():
    rules = RulesConfig(
        ruleset="cwn",
        ability_score_names=_NEON_FLAVOR,
        cwn=CwnConfig(attribute_map=_NEON_AMAP),
    )
    assert rules.cwn is not None
    assert rules.cwn.attribute_map["INTELLIGENCE"] == "Tech"


def test_rules_cwn_with_no_config_block_fails_loud():
    # ruleset='cwn' but no cwn block: validator auto-populates an empty CwnConfig,
    # then rejects it for the missing attribute_map (no silent default).
    with pytest.raises(ValidationError, match="attribute_map"):
        RulesConfig(ruleset="cwn", ability_score_names=_NEON_FLAVOR)
