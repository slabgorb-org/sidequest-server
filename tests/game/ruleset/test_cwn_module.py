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


from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.genre.models.rules import BeatDef


_C = CwnRulesetModule()


def test_cwn_slug():
    assert _C.slug == "cwn"


def test_cwn_luck_save_has_no_attribute_modifier():
    # Luck: target = save_base - (level-1), no attribute mod. At level 3, 15 - 2 = 13.
    cfg = CwnConfig(attribute_map=_NEON_AMAP)
    p = _C.save_params(stats={"Body": 18, "Cool": 18}, save="luck", level=3, label="Luck save", cfg=cfg)
    assert (p.sides, p.count) == (20, 1)
    assert p.modifier == 0  # high stats are irrelevant to Luck
    assert p.difficulty == 13


def test_cwn_physical_save_inherits_swn_best_of_two():
    # Physical: better of STR(Brawn)/CON(Body). Body=14 -> +1. Level 1 -> target 15.
    cfg = CwnConfig(attribute_map=_NEON_AMAP)
    p = _C.save_params(stats={"Brawn": 8, "Body": 14}, save="physical", level=1, label="Physical save", cfg=cfg)
    assert p.modifier == 1
    assert p.difficulty == 15


def test_cwn_inherits_swn_attack_params_vs_ac():
    beat = BeatDef.model_validate(
        {"id": "shoot", "label": "Shoot", "kind": "strike", "base": 0,
         "stat_check": "Reflex", "combat_skill": 1, "attack_bonus": 2}
    )

    class _Core:
        armor_class = 13

    params = _C.attack_params(beat=beat, attacker_stats={"Reflex": 14}, attacker_core=None, target_core=_Core())
    assert params.modifier == 2 + 1 + 1  # attack_bonus + combat_skill + DEX(Reflex) mod
    assert params.target_number == 13
