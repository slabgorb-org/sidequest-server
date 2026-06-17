"""seed_chargen_resources parity with the legacy module-level seed_* functions."""

from __future__ import annotations

from sidequest.game.ruleset import get_ruleset_module
from sidequest.genre.models.rules import RulesConfig


def _wwn_rules() -> RulesConfig:
    return RulesConfig.model_validate(
        {
            "ruleset": "wwn",
            "stat_generation": "standard_array",
            "standard_array": [14, 12, 11, 10, 9, 7],
            "ability_score_names": ["STR", "DEX", "CON", "INT", "WIS", "CHA"],
            "wwn": {
                "attribute_map": {
                    "STRENGTH": "STR",
                    "DEXTERITY": "DEX",
                    "CONSTITUTION": "CON",
                    "INTELLIGENCE": "INT",
                    "WISDOM": "WIS",
                    "CHARISMA": "CHA",
                }
            },
        }
    )


def test_wn_core_seed_resources_empty_for_non_magic_class():
    module = get_ruleset_module("wwn")
    res = module.seed_chargen_resources(rules=_wwn_rules(), stats={"INT": 14}, class_def=None)
    assert res.effort == {}
    assert res.spellcasting is None
    assert res.system_strain is None


def test_dial_seed_resources_is_empty():
    module = get_ruleset_module("dial")
    res = module.seed_chargen_resources(rules=RulesConfig(), stats={}, class_def=None)
    assert res.effort == {} and res.spellcasting is None and res.system_strain is None
