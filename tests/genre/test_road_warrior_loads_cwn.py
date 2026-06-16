"""road_warrior binds ``ruleset: cwn`` with the standard CWN six and ablative-HP
driver combat (Story 86-1, Plan 1 of the road_warrior→CWN epic).

Design: docs/superpowers/specs/2026-06-04-road-warrior-cwn-rig-combat-design.md §6.1.

These are RED until Dev rewrites ``genre_packs/road_warrior/rules.yaml``:
  * add ``ruleset: cwn`` (today it runs the native dial engine — no ruleset line);
  * replace the flavor six (Grip/Iron/Nerve/Scrap/Road Sense/Swagger) with the
    standard CWN six (STR/DEX/CON/INT/WIS/CHA) per decision D3, with an
    identity-style ``cwn.attribute_map`` (CWN canonical attr -> the standard
    abbreviation) — cwn REQUIRES a complete six-key map (RulesConfig._validate_cwn);
  * remove the ``edge_config`` block (Driver Edge / ADR-078 retired → ablative HP, D2);
  * move the ``combat`` confrontation off ``resolution_mode: opposed_check`` to
    ``beat_selection`` + ``win_condition: hp_depletion`` carrying ``opponent_default_stats``
    with all six abilities + the reserved combat-seed keys (hp, armor_class, dexterity);
  * remap every STRUCTURED stat reference (opponent_default_stats keys, beat
    ``stat_check`` values) from the flavor names to the standard six.

Surgical, not grep-the-prose: the spec explicitly permits the narrator to still
*call* DEX "grip" in prose (§6.3). So these assertions key only on STRUCTURED stat
fields (ability_score_names, attribute_map, opponent_default_stats keys, beat
stat_check) where a flavor name is unambiguously a dead reference — never on raw
text, which would be falsely RED on legitimate flavor prose ("grip the wheel").

Pattern precedent: tests/genre/test_neon_loads_cwn.py (the neon_dystopia→CWN binding).
"""

from __future__ import annotations

import pytest

from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import OPPONENT_RESERVED_STAT_KEYS, CwnConfig, WinCondition
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound
from tests.genre.test_resolution_mode import load_pack

# Decision D3: the sheet uses the standard CWN six abbreviations.
STANDARD_SIX = {"STR", "DEX", "CON", "INT", "WIS", "CHA"}
# The retired flavor names — must not survive in any structured stat field.
FLAVOR_NAMES = {"Grip", "Iron", "Nerve", "Scrap", "Road Sense", "Swagger"}
# CWN canonical attribute keys the attribute_map must cover (RulesConfig._validate_cwn).
CWN_CANONICAL_KEYS = {
    "STRENGTH",
    "DEXTERITY",
    "CONSTITUTION",
    "INTELLIGENCE",
    "WISDOM",
    "CHARISMA",
}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load() -> GenrePack:
    if not _has_real_content():
        pytest.skip("sidequest-content not on disk")
    try:
        return load_pack("road_warrior")
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _combat_confrontation(pack: GenrePack):
    """The single ``category == 'combat'`` ConfrontationDef. Fails loudly if the
    pack ships zero or more than one so the downstream assertions can't pass
    vacuously."""
    combats = [c for c in pack.rules.confrontations if c.category == "combat"]
    assert len(combats) == 1, (
        f"road_warrior must ship exactly one combat confrontation for these "
        f"assertions to be meaningful; found {len(combats)}: "
        f"{[c.confrontation_type for c in combats]}"
    )
    return combats[0]


def _win_condition_value(cdef) -> str:
    wc = cdef.win_condition
    return wc.value if hasattr(wc, "value") else wc


# ---------------------------------------------------------------------------
# AC1 — pack binds the cwn ruleset and resolves to the CWN module
# ---------------------------------------------------------------------------


def test_road_warrior_binds_cwn() -> None:
    """road_warrior.rules.ruleset == 'cwn', the cwn block parses, and the bound
    slug resolves to CwnRulesetModule through the production registry."""
    pack = _load()

    assert pack.rules.ruleset == "cwn", (
        f"road_warrior must declare 'ruleset: cwn' (it runs the native dial engine "
        f"today); got ruleset={pack.rules.ruleset!r}"
    )
    assert isinstance(pack.rules.cwn, CwnConfig)
    # pack -> registry wiring: the bound slug must resolve to the CWN module.
    assert isinstance(get_ruleset_module(pack.rules.ruleset), CwnRulesetModule)
    # The accessor the dispatcher calls returns the cwn block, not None.
    assert pack.rules.ruleset_config() is pack.rules.cwn


# ---------------------------------------------------------------------------
# AC2 — standard CWN six on the sheet; flavor names gone (D3)
# ---------------------------------------------------------------------------


def test_road_warrior_uses_standard_cwn_six() -> None:
    """ability_score_names is exactly the standard CWN six; none of the retired
    flavor names survive on the sheet (decision D3)."""
    pack = _load()
    declared = set(pack.rules.ability_score_names)

    assert declared == STANDARD_SIX, (
        f"road_warrior must adopt the standard CWN six {sorted(STANDARD_SIX)} "
        f"(D3 'adopt the standard six; drop the flavor names'); got "
        f"{sorted(declared)}"
    )
    assert not (declared & FLAVOR_NAMES), (
        f"retired flavor stat names must not remain in ability_score_names; "
        f"found {sorted(declared & FLAVOR_NAMES)}"
    )


def test_road_warrior_attribute_map_is_complete_identity_style() -> None:
    """The cwn attribute_map covers all six CWN canonical keys and maps each to a
    declared standard-six ability (the identity-style map D3 calls 'zero
    attribute-map risk'). No flavor name appears as a mapped value."""
    pack = _load()
    assert isinstance(pack.rules.cwn, CwnConfig)
    amap = pack.rules.cwn.attribute_map

    assert set(amap.keys()) >= CWN_CANONICAL_KEYS, (
        f"cwn.attribute_map must cover all six CWN canonical attributes "
        f"{sorted(CWN_CANONICAL_KEYS)}; missing "
        f"{sorted(CWN_CANONICAL_KEYS - set(amap.keys()))}"
    )
    declared = set(pack.rules.ability_score_names)
    assert set(amap.values()) <= declared, (
        f"every attribute_map value must be a declared ability score; offending "
        f"{sorted(set(amap.values()) - declared)}"
    )
    assert not (set(amap.values()) & FLAVOR_NAMES), (
        f"no flavor stat name may survive as an attribute_map value; found "
        f"{sorted(set(amap.values()) & FLAVOR_NAMES)}"
    )


# ---------------------------------------------------------------------------
# AC3 — Driver Edge removed (ablative HP, D2)
# ---------------------------------------------------------------------------


def test_road_warrior_drops_edge_config() -> None:
    """The driver is now an ablative-HP CWN character; the ADR-078 edge_config
    block (Driver Edge) must be gone (decision D2)."""
    pack = _load()
    assert pack.rules.edge_config is None, (
        "road_warrior must remove edge_config — the driver runs on ablative HP "
        "(D2, the SWN-crunch/HP-reintroduction mandate), not the retired Edge pool"
    )


# ---------------------------------------------------------------------------
# AC4 — combat is hp_depletion with a fully-seeded opponent (six + reserved keys)
# ---------------------------------------------------------------------------


def test_road_warrior_combat_is_hp_depletion() -> None:
    """The combat confrontation resolves on HP depletion (CWN personal combat),
    not the native dual-dial opposed_check."""
    pack = _load()
    cdef = _combat_confrontation(pack)
    assert _win_condition_value(cdef) == "hp_depletion", (
        f"road_warrior combat must use win_condition: hp_depletion (CWN ablative "
        f"combat); got {_win_condition_value(cdef)!r}"
    )
    assert cdef.win_condition == WinCondition.hp_depletion


def test_road_warrior_opponent_carries_all_six_plus_reserved_keys() -> None:
    """opponent_default_stats carries all six standard abilities AND the reserved
    combat-seed keys (hp, armor_class, dexterity) the hp_depletion validator and
    SWN initiative require — and uses NO flavor names as ability keys."""
    pack = _load()
    cdef = _combat_confrontation(pack)
    ods = cdef.opponent_default_stats or {}
    assert ods, "combat confrontation must author opponent_default_stats"

    # Reserved combat-seed keys must all be present (load-time requirement for
    # category=combat hp_depletion; dexterity seeds 1d8+DEX initiative).
    for reserved in ("hp", "armor_class", "dexterity"):
        assert reserved in ods, (
            f"hp_depletion combat opponent_default_stats must seed {reserved!r}; "
            f"got keys {sorted(ods)}"
        )

    ability_keys = {k for k in ods if k not in OPPONENT_RESERVED_STAT_KEYS}
    assert ability_keys == STANDARD_SIX, (
        f"opponent_default_stats must carry all six standard abilities (needed for "
        f"spell/physical saves); got ability keys {sorted(ability_keys)}"
    )
    assert not (ability_keys & FLAVOR_NAMES), (
        f"opponent ability keys must not use retired flavor names; found "
        f"{sorted(ability_keys & FLAVOR_NAMES)}"
    )


# ---------------------------------------------------------------------------
# AC5 — structured stat remap sweep: every beat stat_check uses the standard six
# ---------------------------------------------------------------------------


def test_all_beat_stat_checks_use_standard_six() -> None:
    """Every beat ``stat_check`` across ALL confrontations references a standard-six
    ability — no dead flavor stat name (Grip/Iron/Road Sense/...) survives in a
    structured stat field. This is the remap-sweep guard; it keys on stat_check
    (a structured reference), never on prose, so flavor narration is unaffected."""
    pack = _load()
    declared = set(pack.rules.ability_score_names)

    offending: list[tuple[str, str, str]] = []
    for cdef in pack.rules.confrontations:
        for beat in cdef.beats:
            stat = getattr(beat, "stat_check", None)
            if stat is None:
                continue
            if stat in FLAVOR_NAMES or stat not in declared:
                offending.append((cdef.confrontation_type, beat.id, stat))

    assert not offending, (
        f"every beat stat_check must reference a declared standard-six ability; "
        f"dead/flavor stat_check references (confrontation, beat, stat): {offending}"
    )
