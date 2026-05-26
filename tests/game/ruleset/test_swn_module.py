from sidequest.game.creature_core import CreatureCore, HpPool


def _core(*, name="Mara", ac=10, **kw):
    return CreatureCore(
        name=name, description="d", personality="p",
        hp=HpPool(current=8, max=8, base_max=8), armor_class=ac, **kw,
    )


def test_creature_core_has_armor_class_default_10():
    core = CreatureCore(name="x", description="d", personality="p")
    assert core.armor_class == 10


def test_creature_core_armor_class_settable():
    assert _core(ac=15).armor_class == 15


# SwnConfig tests (Task 5) — SRD-sourced constants verified from PDF pp. 46-47
from sidequest.genre.models.rules import RulesConfig, SwnConfig


def test_rules_swn_config_defaults():
    rules = RulesConfig(ruleset="swn")
    assert rules.swn is not None
    assert rules.swn.unarmored_ac == 10
    # SRD p.46: "saving throw scores start at 15, decrease by one point each time
    # you advance a level" — save_base=15 is the level-1 target before attribute mod.
    assert rules.swn.save_base == 15


def test_rules_swn_config_absent_for_native():
    assert RulesConfig().swn is None


# SwnRulesetModule tests (Task 6) — modifier curve + attack_params
import pytest
from sidequest.game.ruleset.swn import SwnRulesetModule, swn_attribute_modifier
from sidequest.genre.models.rules import BeatDef

_S = SwnRulesetModule()


@pytest.mark.parametrize(
    "score,mod",
    [(3, -2), (4, -1), (7, -1), (8, 0), (13, 0), (14, 1), (17, 1), (18, 2)],
)
def test_swn_modifier_curve(score, mod):
    assert swn_attribute_modifier(score) == mod


def test_swn_attack_params_uses_target_ac_and_attack_bonus():
    beat = BeatDef.model_validate(
        {
            "id": "shoot",
            "label": "Shoot",
            "kind": "strike",
            "base": 0,
            "stat_check": "DEXTERITY",
            "combat_skill": 1,
            "attack_bonus": 2,
        }
    )
    attacker_stats = {"DEXTERITY": 14}  # +1 SWN modifier
    target = _core(ac=13)
    params = _S.attack_params(
        beat=beat,
        attacker_stats=attacker_stats,
        attacker_core=None,
        target_core=target,
    )
    assert params.modifier == 2 + 1 + 1  # attack_bonus + combat_skill + DEX mod = 4
    assert params.target_number == 13  # target AC
