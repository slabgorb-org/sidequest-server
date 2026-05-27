import random

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


def test_roll_2d6_plus1_is_in_range():
    """Result must be in [3, 13] (min 2+1, max 12+1)."""
    spec = DamageSpec(dice="2d6", bonus=1)
    rng = random.Random(0)
    result = spec.roll(rng)
    assert 3 <= result <= 13, f"2d6+1 roll {result!r} out of [3, 13]"


def test_roll_2d6_plus1_seed0_is_deterministic():
    """Same seed must produce the same result every call."""
    spec = DamageSpec(dice="2d6", bonus=1)
    rng_a = random.Random(0)
    rng_b = random.Random(0)
    assert spec.roll(rng_a) == spec.roll(rng_b)


def test_roll_1d4_no_bonus_is_in_range():
    """1d4 with no bonus must be in [1, 4]."""
    spec = DamageSpec(dice="1d4")
    result = spec.roll(random.Random(42))
    assert 1 <= result <= 4


def test_roll_applies_bonus():
    """bonus=5 on a 1d4 must always add 5 to every result."""
    spec = DamageSpec(dice="1d4", bonus=5)
    for seed in range(20):
        result = spec.roll(random.Random(seed))
        assert 6 <= result <= 9, f"seed={seed} got {result}, expected 6-9"
