from sidequest.game.ruleset.swn import SwnRulesetModule


class _Cfg:
    # Mirrors SwnConfig.attribute_map shape used by save_params.
    attribute_map = {
        "STRENGTH": "Physique",
        "CONSTITUTION": "Physique",
        "DEXTERITY": "Reflex",
        "INTELLIGENCE": "Intellect",
        "WISDOM": "Resolve",
        "CHARISMA": "Cunning",
    }


def test_ship_attack_params_uses_better_of_int_dex():
    mod = SwnRulesetModule()
    # Reflex 16 -> +1 (DEX maps to Reflex), Intellect 10 -> 0 (INT maps to Intellect):
    # SWN tight curve: 14-17 -> +1, 8-13 -> 0. Better is +1.
    params = mod.ship_attack_params(
        attacker_stats={"Reflex": 16, "Intellect": 10},
        pilot_skill=1,
        attack_bonus=1,
        geometry_modifier=2,
        target_ac=16,
        cfg=_Cfg(),
    )
    # attack_bonus(1) + pilot_skill(1) + better_mod(+1) + geometry(2) = 5
    assert params.modifier == 5
    assert params.target_number == 16


def test_ship_attack_params_negative_geometry():
    mod = SwnRulesetModule()
    # Reflex 8 -> 0, Intellect 8 -> 0: both in 8-13 range -> 0 mod. Best is 0.
    params = mod.ship_attack_params(
        attacker_stats={"Reflex": 8, "Intellect": 8},
        pilot_skill=0,
        attack_bonus=0,
        geometry_modifier=-6,
        target_ac=16,
        cfg=_Cfg(),
    )
    # 0 + 0 + 0 + (-6) = -6
    assert params.modifier == -6
    assert params.target_number == 16
