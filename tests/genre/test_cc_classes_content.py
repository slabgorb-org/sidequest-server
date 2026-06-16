"""Verify caverns_and_claudes loads four classic classes from classes.yaml."""

from sidequest.genre.loader import GenreLoader


def test_cc_loads_four_classes():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    ids = {c.id for c in pack.classes}
    assert ids == {"fighter", "mage", "cleric", "thief"}


def test_cc_class_prime_requisites_distinct():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    primes = sorted(c.prime_requisite for c in pack.classes)
    assert primes == ["DEX", "INT", "STR", "WIS"]


def test_cc_class_kit_tables_named_correctly():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    by_id = {c.id: c for c in pack.classes}
    assert by_id["fighter"].kit_table == "fighter_kit"
    assert by_id["mage"].kit_table == "mage_kit"
    assert by_id["cleric"].kit_table == "cleric_kit"
    assert by_id["thief"].kit_table == "thief_kit"
