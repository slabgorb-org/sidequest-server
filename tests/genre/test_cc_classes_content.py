"""Verify caverns_and_claudes loads the three WWN Callings from classes.yaml.

WWN port (2026-06-12): the B/X Fighter/Cleric/Thief/Mage roster was replaced by
the faithful WWN 3-chassis port (Warrior/Expert/Mage), mirroring heavy_metal.
"""

from sidequest.genre.loader import GenreLoader


def test_cc_loads_three_callings():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    ids = {c.id for c in pack.classes}
    assert ids == {"warrior", "expert", "mage"}


def test_cc_class_prime_requisites_distinct():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    primes = sorted(c.prime_requisite for c in pack.classes)
    assert primes == ["DEX", "INT", "STR"]


def test_cc_class_kit_tables_named_correctly():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    by_id = {c.id: c for c in pack.classes}
    assert by_id["warrior"].kit_table == "warrior_kit"
    assert by_id["expert"].kit_table == "expert_kit"
    assert by_id["mage"].kit_table == "mage_kit"
