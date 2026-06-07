import pytest
from pydantic import ValidationError

from sidequest.genre.models.inventory import CatalogItem, DamageSpec


def test_weapon_carries_swn_native_damage_dice():
    item = CatalogItem(id="mag_pistol", name="Mag Pistol", description="x",
                       category="weapon", damage=DamageSpec(dice="1d6", bonus=1))
    assert item.damage.dice == "1d6"
    assert item.damage.bonus == 1


def test_armor_carries_flat_mitigation():
    item = CatalogItem(id="armored_vac", name="Armored Vacc Suit", description="x",
                       category="armor", mitigation=2)
    assert item.mitigation == 2


def test_unparseable_or_unsupported_damage_dice_rejected_at_load():
    with pytest.raises(ValidationError):
        DamageSpec(dice="banana")
    with pytest.raises(ValidationError):
        DamageSpec(dice="1d7")   # d7 not a supported DieSides face count


def test_damage_absent_by_default():
    item = CatalogItem(id="ration", name="Ration", description="x", category="consumable")
    assert item.damage is None and item.mitigation is None
