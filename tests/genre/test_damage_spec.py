import pytest
from pydantic import ValidationError

from sidequest.genre.models.inventory import DamageSpec


def test_damage_spec_defaults_armor_piercing_zero():
    spec = DamageSpec(dice="1d4")
    assert spec.armor_piercing == 0


def test_damage_spec_accepts_armor_piercing():
    spec = DamageSpec(dice="1d4", armor_piercing=20)
    assert spec.armor_piercing == 20


def test_damage_spec_rejects_negative_armor_piercing():
    with pytest.raises(ValidationError):
        DamageSpec(dice="1d4", armor_piercing=-1)
