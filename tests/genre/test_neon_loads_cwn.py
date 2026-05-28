"""neon_dystopia binds ``ruleset: cwn`` and loads with a complete attribute_map.

Task 7 of the neon_dystopia -> CWN binding plan. Proves the pack loads as a
whole under the CWN ruleset (RulesConfig._validate_cwn must accept the authored
six-key attribute_map mapping each CWN attribute to a declared flavor stat).
"""

from __future__ import annotations

import pytest

from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import CwnConfig
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound
from tests.genre.test_resolution_mode import load_pack


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_neon_binds_cwn_with_attribute_map() -> None:
    try:
        pack: GenrePack = load_pack("neon_dystopia")
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))

    assert pack.rules.ruleset == "cwn"
    assert isinstance(pack.rules.cwn, CwnConfig)

    amap = pack.rules.cwn.attribute_map
    assert amap["INTELLIGENCE"] == "Tech"
    assert amap["CONSTITUTION"] == "Body"

    # Every mapped flavor stat must be a declared ability score (validator contract).
    declared = set(pack.rules.ability_score_names)
    assert set(amap.values()) <= declared

    # The bound module resolves to the CWN module — proves pack -> registry wiring.
    assert isinstance(get_ruleset_module(pack.rules.ruleset), CwnRulesetModule)

    # ruleset_config() returns the cwn block (the accessor dispatch now calls).
    assert pack.rules.ruleset_config() is pack.rules.cwn
