"""Verify caverns_and_claudes edge_config covers the four classic classes."""

from sidequest.genre.loader import GenreLoader


def test_cc_edge_config_present():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    assert pack.rules.edge_config is not None


def test_cc_edge_config_covers_four_classes():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    bmbc = pack.rules.edge_config.base_max_by_class
    assert set(bmbc.keys()) >= {"Fighter", "Mage", "Cleric", "Thief"}


def test_cc_fighter_has_most_edge():
    loader = GenreLoader()
    pack = loader.load("caverns_and_claudes")
    bmbc = pack.rules.edge_config.base_max_by_class
    assert bmbc["Fighter"] > bmbc["Mage"]
    assert bmbc["Fighter"] >= bmbc["Cleric"]
    assert bmbc["Cleric"] >= bmbc["Thief"]
