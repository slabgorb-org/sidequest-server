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
