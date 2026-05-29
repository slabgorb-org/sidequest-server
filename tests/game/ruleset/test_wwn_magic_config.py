from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import MagicConfig, RulesConfig, WwnConfig

_EH_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}
_EH_FLAVOR = ["Strength", "Agility", "Endurance", "Insight", "Spirit", "Harmony"]


def test_magic_config_defaults():
    m = MagicConfig()
    assert m.killing_blow_divisor == 2  # ceil(level/2)
    assert m.day_reclaim_requires_comfort is True
    assert m.default_spell_save == "mental"
    assert m.effort_base == 1  # max = effort_base + skill + mod


def test_wwn_config_has_magic_default():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    assert cfg.magic.default_spell_save == "mental"  # default_factory


def test_magic_default_spell_save_must_be_valid():
    with pytest.raises(ValidationError, match="default_spell_save"):
        RulesConfig(
            ruleset="wwn",
            ability_score_names=_EH_FLAVOR,
            wwn=WwnConfig(attribute_map=_EH_AMAP, magic=MagicConfig(default_spell_save="luck_XYZ")),
        )


def test_wwn_accepts_valid_magic_block():
    rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=_EH_FLAVOR,
        wwn=WwnConfig(attribute_map=_EH_AMAP, magic=MagicConfig(default_spell_save="evasion")),
    )
    assert rules.wwn.magic.default_spell_save == "evasion"
