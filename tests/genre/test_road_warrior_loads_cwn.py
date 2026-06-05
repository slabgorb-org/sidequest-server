"""road_warrior binds ``ruleset: cwn`` with the STANDARD CWN six — Story 86-1 Plan 1.

Unlike the other CWN/SWN-bound packs (neon_dystopia, space_opera, the AWN
fixtures), road_warrior does NOT keep flavor stat names behind an attribute_map.
Per design decision D3 (docs/superpowers/specs/2026-06-04-road-warrior-cwn-rig-combat-design.md,
§3 & §6.1) it **adopts the standard six on the sheet** (STR/DEX/CON/INT/WIS/CHA)
and **drops** the old flavor names (Grip/Iron/Nerve/Scrap/Road Sense/Swagger) —
"a port is a port", zero attribute-map risk.

That decision still has to satisfy ``RulesConfig._validate_cwn`` (rules.py), which
REQUIRES a complete six-key attribute_map even when the sheet already uses the
canonical labels. So "zero attribute-map risk" means the map is the obvious
identity-ish map (STRENGTH->STR, DEXTERITY->DEX, ...), NOT an absent map. These
tests pin both halves: the standard six on the sheet AND the identity map under
``cwn.attribute_map``.

RED until Plan 1 lands: road_warrior currently runs the ``native`` dial engine
with flavor stat names, an ``edge_config`` Driver-Edge block, and an
``opposed_check`` combat confrontation. Each assertion below fails against that
state and passes once the pack is migrated.

Mirrors the binding-proof shape of ``tests/genre/test_neon_loads_cwn.py``.
"""

from __future__ import annotations

import pytest

from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    ConfrontationDef,
    CwnConfig,
    ResolutionMode,
    WinCondition,
)
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound
from tests.genre.test_resolution_mode import load_pack

# The standard CWN six, as they must appear on the road_warrior sheet (D3 / §6.1).
STANDARD_SIX = {"STR", "DEX", "CON", "INT", "WIS", "CHA"}
# The flavor names that MUST be gone after the migration.
OLD_FLAVOR_NAMES = {"Grip", "Iron", "Nerve", "Scrap", "Road Sense", "Swagger"}
# The canonical attribute keys every *_validate_cwn map must carry.
CANONICAL_ATTRS = {
    "STRENGTH",
    "CONSTITUTION",
    "DEXTERITY",
    "INTELLIGENCE",
    "WISDOM",
    "CHARISMA",
}
# The obvious identity map the spec calls "zero attribute-map risk".
EXPECTED_ATTRIBUTE_MAP = {
    "STRENGTH": "STR",
    "DEXTERITY": "DEX",
    "CONSTITUTION": "CON",
    "INTELLIGENCE": "INT",
    "WISDOM": "WIS",
    "CHARISMA": "CHA",
}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _road_warrior() -> GenrePack:
    try:
        return load_pack("road_warrior")
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _combat_confrontation(pack: GenrePack) -> ConfrontationDef:
    """The pack's ``combat`` confrontation, or fail loudly if absent."""
    combat = next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )
    assert combat is not None, (
        "road_warrior must declare a 'combat' confrontation; "
        f"found types: {[c.confrontation_type for c in pack.rules.confrontations]}"
    )
    return combat


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_binds_cwn_module() -> None:
    """The pack declares ``ruleset: cwn`` and that slug resolves, in the
    production registry, to the CWN module — proving pack -> registry wiring."""
    pack = _road_warrior()

    assert pack.rules.ruleset == "cwn", (
        f"road_warrior must bind ruleset 'cwn' (Plan 1); got {pack.rules.ruleset!r}"
    )
    assert isinstance(pack.rules.cwn, CwnConfig), (
        "a cwn-bound pack must carry a CwnConfig under rules.cwn; "
        f"got {type(pack.rules.cwn).__name__}"
    )
    # Registry wiring: the bound slug resolves to the CWN module singleton.
    assert isinstance(get_ruleset_module(pack.rules.ruleset), CwnRulesetModule)
    # Accessor dispatch returns the cwn block (what dispatch.dice calls).
    assert pack.rules.ruleset_config() is pack.rules.cwn


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_adopts_standard_cwn_six() -> None:
    """The sheet uses the standard six (D3) and NONE of the old flavor names."""
    pack = _road_warrior()
    declared = set(pack.rules.ability_score_names)

    assert declared == STANDARD_SIX, (
        "road_warrior ability_score_names must be exactly the standard CWN six "
        f"{sorted(STANDARD_SIX)}; got {sorted(declared)}"
    )
    leftover_flavor = declared & OLD_FLAVOR_NAMES
    assert not leftover_flavor, (
        "the old Driver-Edge flavor stat names must be fully removed from the "
        f"sheet (D3); still present: {sorted(leftover_flavor)}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_cwn_attribute_map_is_identity_standard_six() -> None:
    """The validator still demands a complete six-key map; for the standard six
    it must be the obvious identity map (STRENGTH->STR, ...), and every value
    must be a declared ability score (the validator's own contract)."""
    pack = _road_warrior()
    assert isinstance(pack.rules.cwn, CwnConfig)
    amap = pack.rules.cwn.attribute_map

    assert amap.keys() >= CANONICAL_ATTRS, (
        "cwn.attribute_map must carry all six canonical attribute keys; "
        f"missing: {sorted(CANONICAL_ATTRS - amap.keys())}"
    )
    for canonical, expected in EXPECTED_ATTRIBUTE_MAP.items():
        assert amap.get(canonical) == expected, (
            f"cwn.attribute_map[{canonical!r}] must be {expected!r} (identity map, "
            f"zero attribute-map risk); got {amap.get(canonical)!r}"
        )
    declared = set(pack.rules.ability_score_names)
    assert set(amap.values()) <= declared, (
        "every attribute_map value must be a declared ability score; "
        f"orphans: {sorted(set(amap.values()) - declared)}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_drops_driver_edge_config() -> None:
    """Driver Edge (ADR-078 edge_config) is removed — the driver is now an
    ablative-HP CWN character (D2), not an Edge-pool character."""
    pack = _road_warrior()
    assert pack.rules.edge_config is None, (
        "road_warrior must drop edge_config / Driver Edge (D2: driver is full "
        "ablative HP); edge_config is still present"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_combat_is_hp_depletion_beat_selection() -> None:
    """The combat confrontation resolves on HP-to-0 (CWN personal combat), not
    on an opposed-check dial."""
    pack = _road_warrior()
    combat = _combat_confrontation(pack)

    assert combat.category == "combat"
    assert combat.win_condition == WinCondition.hp_depletion, (
        "road_warrior combat must use win_condition 'hp_depletion' (§6.1); "
        f"got {combat.win_condition.value!r}"
    )
    assert combat.resolution_mode == ResolutionMode.beat_selection, (
        "hp_depletion CWN combat resolves via beat_selection (HP-to-0), not "
        f"opposed_check; got {combat.resolution_mode.value!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_combat_opponent_stats_carry_six_plus_reserved() -> None:
    """The documented gotcha: an hp_depletion combat's opponent_default_stats must
    carry the reserved combat keys (hp/armor_class/dexterity) AND all six ability
    scores (for saves). The ConfrontationDef load-time validator already enforces
    the reserved keys; this additionally pins the all-six-for-saves requirement."""
    pack = _road_warrior()
    combat = _combat_confrontation(pack)
    ods = combat.opponent_default_stats or {}

    # Reserved combat seeds.
    assert "hp" in ods and int(ods["hp"]) >= 1, f"opponent_default_stats needs hp>=1; got {ods!r}"
    assert "armor_class" in ods and int(ods["armor_class"]) >= 1, (
        f"opponent_default_stats needs armor_class>=1; got {ods!r}"
    )
    assert "dexterity" in ods and int(ods["dexterity"]) >= 3, (
        f"opponent_default_stats needs the reserved SWN-initiative dexterity>=3; got {ods!r}"
    )

    # All six ability scores (reserved keys stripped) — the saves gotcha.
    ability_scores = combat.opponent_ability_scores() or {}
    assert set(ability_scores.keys()) == STANDARD_SIX, (
        "opponent_default_stats must carry ALL SIX standard ability scores "
        f"(needed for CWN saves); got ability keys {sorted(ability_scores.keys())}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_pack_loads_with_dual_dial_schema() -> None:
    """Migration guard mirroring test_space_opera_pack_loads_with_dual_dial_schema:
    once combat moves to metricless hp_depletion, the SURVIVING dial confrontations
    (negotiation, chase) must still carry valid dual-dial metrics. Filter on
    win_condition so the metricless combat is skipped rather than NPE-ing on
    ``player_metric.threshold``. At least one dial confrontation must remain so
    this assertion is not vacuous."""
    pack = _road_warrior()
    dial_confrontations = [
        cdef
        for cdef in pack.rules.confrontations
        if (
            cdef.win_condition.value if hasattr(cdef.win_condition, "value") else cdef.win_condition
        )
        == "dial_threshold"
    ]
    assert dial_confrontations, (
        "road_warrior must retain at least one dial_threshold confrontation "
        "(negotiation/chase) for this dual-dial assertion to be meaningful"
    )
    for cdef in dial_confrontations:
        assert cdef.player_metric.threshold > 0
        assert cdef.opponent_metric.threshold > 0
        for beat in cdef.beats:
            kind = beat.kind.value if hasattr(beat.kind, "value") else beat.kind
            assert kind in {"strike", "brace", "push", "angle"}
