from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.inventory import CatalogItem, DamageSpec


def test_damage_spec_trauma_fields_default_off():
    spec = DamageSpec(dice="1d6")
    assert spec.trauma_die is None
    assert spec.trauma_rating == 1
    assert spec.shock == 0
    assert spec.shock_ac is None
    assert spec.trauma_target is None


def test_damage_spec_accepts_trauma_and_shock():
    spec = DamageSpec(
        dice="2d8", trauma_die="1d6", trauma_rating=3, shock=2, shock_ac=15, trauma_target=7
    )
    assert spec.trauma_die == "1d6"
    assert spec.trauma_rating == 3
    assert spec.shock == 2
    assert spec.shock_ac == 15
    assert spec.trauma_target == 7


def test_shock_requires_shock_ac():
    with pytest.raises(ValidationError):
        DamageSpec(dice="1d6", shock=2)


def test_trauma_die_validated_as_dice_notation():
    with pytest.raises(ValidationError):
        DamageSpec(dice="1d6", trauma_die="banana")


def test_trauma_rating_must_be_at_least_one():
    with pytest.raises(ValidationError):
        DamageSpec(dice="1d6", trauma_rating=0)


def test_shock_non_negative():
    with pytest.raises(ValidationError):
        DamageSpec(dice="1d6", shock=-1)


def test_catalog_item_armor_class_optional():
    armor = CatalogItem(
        id="vest", name="Vest", description="x", category="armor", armor_class=15, mitigation=2
    )
    assert armor.armor_class == 15
    assert armor.mitigation == 2
    plain = CatalogItem(id="rock", name="Rock", description="x", category="misc")
    assert plain.armor_class is None
