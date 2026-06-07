import pytest

from sidequest.game.creature_core import HpConfigMissingClassError, hp_pool_from_config


class _Cfg:
    base_max_by_class = {"Fighter": 8, "Mage": 4}


def test_hp_seed_applies_con_modifier_floored_at_one():
    assert hp_pool_from_config(_Cfg(), "Fighter", con_score=17).base_max == 11  # 8 + 3
    assert hp_pool_from_config(_Cfg(), "Mage", con_score=6).base_max == 2  # 4 + (-2)
    assert hp_pool_from_config(_Cfg(), "Mage", con_score=3).base_max == 1  # 4 + (-4) floored 1


def test_missing_class_raises_loudly():
    with pytest.raises(HpConfigMissingClassError):
        hp_pool_from_config(_Cfg(), "Psion", con_score=10)
