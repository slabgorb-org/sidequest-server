"""space_opera binds ``ruleset: swn`` and loads with a complete attribute_map.

Task 7 of the space_opera -> SWN binding plan. Proves the pack still loads as a
whole under the SWN ruleset (RulesConfig._validate_swn must accept the authored
six-key attribute_map mapping each SWN attribute to a declared flavor stat).
"""

from __future__ import annotations

import pytest

from sidequest.genre.models.pack import GenrePack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound
from tests.genre.test_resolution_mode import load_pack


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_space_opera_binds_swn_with_attribute_map() -> None:
    try:
        pack: GenrePack = load_pack("space_opera")
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))

    assert pack.rules.ruleset == "swn"
    assert pack.rules.swn is not None
    amap = pack.rules.swn.attribute_map
    assert amap["CHARISMA"] == "Influence"
    assert amap["STRENGTH"] == "Physique"
    assert amap["CONSTITUTION"] == "Resolve"
    assert amap["DEXTERITY"] == "Reflex"
    assert amap["INTELLIGENCE"] == "Intellect"
    assert amap["WISDOM"] == "Cunning"
    # Every mapped flavor stat must be a declared ability score.
    declared = set(pack.rules.ability_score_names)
    assert set(amap.values()) <= declared
