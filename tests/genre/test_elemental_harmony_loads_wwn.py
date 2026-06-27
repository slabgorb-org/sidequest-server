"""elemental_harmony binds ``ruleset: wwn`` and loads clean with all WWN pieces.

Calibration test (Plan 3, Task 16). Content-gated: skips without
``SIDEQUEST_GENRE_PACKS`` / real pack on disk. Asserts:

1. pack.rules.ruleset == "wwn"
2. WwnConfig is present with the 6-key attribute_map and magic block
3. pack.wwn_spell_catalog is present and non-empty
4. Every caster class's starting_prepared id resolves in the spell catalog
5. The "Martial Exchange" confrontation has a cast_spell beat
6. Guardian has warrior == True; Channeler + Spirit Medium have magic_access == "wwn"
"""

from __future__ import annotations

import pytest

from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import WwnConfig
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound
from tests.genre.test_resolution_mode import load_pack

_WWN_CANONICAL_KEYS = {
    "STRENGTH",
    "DEXTERITY",
    "CONSTITUTION",
    "INTELLIGENCE",
    "WISDOM",
    "CHARISMA",
}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_elemental_harmony_loads_clean_under_wwn() -> None:
    try:
        pack: GenrePack = load_pack("elemental_harmony")
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))

    # 1. ruleset == "wwn"
    assert pack.rules.ruleset == "wwn"

    # 2. WwnConfig is present with complete attribute_map and magic block
    assert isinstance(pack.rules.wwn, WwnConfig)
    amap = pack.rules.wwn.attribute_map
    assert set(amap.keys()) == _WWN_CANONICAL_KEYS, (
        f"attribute_map keys mismatch: got {sorted(amap.keys())}"
    )
    # Canonical WN stat keys — the WN family shares one attribute block; the pack
    # uses the standard STR/DEX/CON/INT/WIS/CHA, not flavor-renamed stats.
    assert amap["STRENGTH"] == "STR"
    assert amap["DEXTERITY"] == "DEX"
    assert amap["CONSTITUTION"] == "CON"
    assert amap["INTELLIGENCE"] == "INT"
    assert amap["WISDOM"] == "WIS"
    assert amap["CHARISMA"] == "CHA"
    # Every mapped flavor stat must be a declared ability score
    declared = set(pack.rules.ability_score_names)
    assert set(amap.values()) <= declared
    # magic block is present
    assert pack.rules.wwn.magic is not None

    # Bound module resolves to WwnRulesetModule
    assert isinstance(get_ruleset_module(pack.rules.ruleset), WwnRulesetModule)

    # 3. wwn_spell_catalog is present and non-empty
    assert pack.wwn_spell_catalog is not None, (
        "wwn_spell_catalog is None — spells_wwn.yaml not loaded"
    )
    assert len(pack.wwn_spell_catalog.spells) > 0, "wwn_spell_catalog has no spells"

    # 4. Every caster class's starting_prepared id resolves in the catalog
    catalog_ids = {s.id for s in pack.wwn_spell_catalog.spells}
    assert pack.classes is not None
    for cls in pack.classes:
        if cls.wwn_magic is None:
            continue
        starting_prepared = getattr(cls.wwn_magic, "starting_prepared", None) or []
        for spell_id in starting_prepared:
            assert spell_id in catalog_ids, (
                f"class {cls.id!r} starting_prepared {spell_id!r} not in wwn_spell_catalog"
            )

    # 5. "Martial Exchange" combat confrontation is de-nativized (108-3 / ADR-143):
    #    every native combat beat — cast_spell included — was stripped off the
    #    hp_depletion combat def, so its beat pool is empty. Under the WWN binding
    #    casting in combat is the synthesized WN cast action routed at dispatch
    #    (epic-152), NOT an authored combat beat; caster capability is carried by
    #    the classes' wwn_magic (asserted in #4 above, which is the real magic
    #    surface). (Loader assertion-flip, story 125-8.)
    martial_exchange = next(
        (c for c in pack.rules.confrontations if c.label == "Martial Exchange"),
        None,
    )
    assert martial_exchange is not None, "No 'Martial Exchange' confrontation found"
    beat_ids = {b.id for b in martial_exchange.beats}
    assert beat_ids == set(), (
        "108-3 strips every native combat beat (cast_spell included) off the WWN "
        "hp_depletion combat def — the WN round owns the action set, so the combat "
        f"def authors zero beats; got {sorted(beat_ids)}"
    )

    # 6. Archetype markers: Guardian is warrior; Channeler + Spirit Medium are wwn casters
    classes_by_id = {c.id: c for c in pack.classes}

    guardian = classes_by_id.get("guardian")
    assert guardian is not None, "guardian class not found"
    assert guardian.warrior is True, "guardian.warrior should be True"

    channeler = classes_by_id.get("channeler")
    assert channeler is not None, "channeler class not found"
    assert channeler.magic_access == "wwn", (
        f"channeler.magic_access = {channeler.magic_access!r}, expected 'wwn'"
    )

    spirit_medium = classes_by_id.get("spirit_medium")
    assert spirit_medium is not None, "spirit_medium class not found"
    assert spirit_medium.magic_access == "wwn", (
        f"spirit_medium.magic_access = {spirit_medium.magic_access!r}, expected 'wwn'"
    )
