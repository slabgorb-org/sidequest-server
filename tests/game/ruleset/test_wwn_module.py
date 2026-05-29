from __future__ import annotations

import pytest

from sidequest.game.ruleset.swn import swn_attribute_modifier
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.genre.models.rules import BeatDef, WwnConfig

_EH_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}
_W = WwnRulesetModule()


def test_wwn_slug():
    assert _W.slug == "wwn"


def test_wwn_inherits_swn_attribute_curve():
    # WWN curve is identical to SWN: 3->-2, 18->+2.
    assert _W.stat_modifier({"Strength": 3}, "Strength") == swn_attribute_modifier(3) == -2
    assert _W.stat_modifier({"Strength": 18}, "Strength") == swn_attribute_modifier(18) == 2


def test_wwn_luck_save_has_no_attribute_modifier():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    p = _W.save_params(
        stats={"Endurance": 18, "Harmony": 18}, save="luck", level=3, label="Luck", cfg=cfg
    )
    assert (p.sides, p.count) == (20, 1)
    assert p.modifier == 0
    assert p.difficulty == 13  # 15 - (3-1)


def test_wwn_physical_save_inherits_swn_best_of_two():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    p = _W.save_params(
        stats={"Strength": 8, "Endurance": 14}, save="physical", level=1, label="Phys", cfg=cfg
    )
    assert p.modifier == 1  # Endurance 14 -> +1, better of Str/Con
    assert p.difficulty == 15


def test_wwn_attack_params_vs_ac():
    beat = BeatDef.model_validate(
        {
            "id": "strike",
            "label": "Strike",
            "kind": "strike",
            "base": 0,
            "stat_check": "Agility",
            "combat_skill": 1,
            "attack_bonus": 2,
        }
    )

    class _Core:
        armor_class = 13

    params = _W.attack_params(
        beat=beat, attacker_stats={"Agility": 14}, attacker_core=None, target_core=_Core()
    )
    assert params.modifier == 2 + 1 + 1
    assert params.target_number == 13


def test_wwn_has_no_ship_gunnery():
    cfg = WwnConfig(attribute_map=_EH_AMAP)
    with pytest.raises(NotImplementedError, match="ship-gunnery"):
        _W.ship_attack_params(
            attacker_stats={"Agility": 14},
            pilot_skill=1,
            attack_bonus=0,
            geometry_modifier=0,
            target_ac=12,
            cfg=cfg,
        )
