"""Characterization net pinning chargen output across the ADR-143 extraction.

Pins builder.generate_stats + seed_chargen_resources byte-identical before and
after the move onto the RulesetModule surface. Synthetic fixtures only.
"""

from __future__ import annotations

import random

from sidequest.game.builder import CharacterBuilder
from sidequest.game.ruleset import get_ruleset_module
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    ClassDef,
    MechanicalEffects,
    WwnClassMagic,
    WwnEffortSource,
)
from sidequest.genre.models.rules import RulesConfig

# ---------------------------------------------------------------------------
# Minimal synthetic fixture helpers (inlined — do NOT import test internals)
# ---------------------------------------------------------------------------


WWN_ABILITY_NAMES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]

WWN_ATTRIBUTE_MAP = {
    "STRENGTH": "STR",
    "DEXTERITY": "DEX",
    "CONSTITUTION": "CON",
    "INTELLIGENCE": "INT",
    "WISDOM": "WIS",
    "CHARISMA": "CHA",
}


def _wwn_rules() -> RulesConfig:
    return RulesConfig.model_validate(
        {
            "ruleset": "wwn",
            "stat_generation": "standard_array",
            "standard_array": [14, 12, 11, 10, 9, 7],
            "ability_score_names": WWN_ABILITY_NAMES,
            "wwn": {"attribute_map": WWN_ATTRIBUTE_MAP},
        }
    )


def _make_choice(label: str, description: str = "desc") -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description=description,
        mechanical_effects=MechanicalEffects(),
    )


def _one_choice_scenes() -> list[CharCreationScene]:
    return [
        CharCreationScene(
            id="pick",
            title="T",
            narration="N",
            choices=[_make_choice("Go")],
        )
    ]


def _high_mage_class() -> ClassDef:
    """A minimal synthetic WWN magic class (single WISDOM-governed Effort source).

    effort_base (1, WwnConfig default) + starting_skill_level (1) +
    swn_attribute_modifier(WIS score) → pool max. casts/max-spell-level tables
    populated at level 1, plus two starting_prepared spell ids (no
    prepared_by_level key → no truncation).
    """
    return ClassDef(
        id="high_mage",
        display_name="High Mage",
        rpg_role="caster",
        jungian_default="Sage",
        prime_requisite="WIS",
        minimum_score=9,
        kit_table="mage_kit",
        wwn_magic=WwnClassMagic(
            effort_sources=[
                WwnEffortSource(
                    source="high_mage",
                    governing_attr="WISDOM",
                    relevant_skill="Magic",
                    starting_skill_level=1,
                ),
            ],
            casts_per_day_by_level={"1": 2},
            max_spell_level_by_level={"1": 1},
            starting_prepared=["magic_dart", "ward"],
        ),
    )


# ---------------------------------------------------------------------------
# seed_chargen_resources — pinned behavior (migrated from seed_system_strain +
# seed_wwn_magic, ADR-143 Task 2)
# ---------------------------------------------------------------------------


def test_seed_chargen_resources_no_system_strain_for_wwn() -> None:
    """WWN has no System Strain (that's a CWN/AWN mechanic). Must return None."""
    rules = _wwn_rules()
    module = get_ruleset_module("wwn")
    res = module.seed_chargen_resources(rules=rules, stats={"CON": 11}, class_def=None)
    assert res.system_strain is None


def test_seed_chargen_resources_no_system_strain_for_various_con_scores() -> None:
    """Multiple CON scores — all must be None system_strain for WWN (no System Strain)."""
    rules = _wwn_rules()
    module = get_ruleset_module("wwn")
    for con in [7, 10, 14, 18]:
        res = module.seed_chargen_resources(rules=rules, stats={"CON": con}, class_def=None)
        assert res.system_strain is None, (
            f"WWN seed_chargen_resources should have no system_strain for CON={con}"
        )


def test_seed_chargen_resources_empty_for_none_class_def() -> None:
    """No class_def → no Effort pools, no spellcasting state."""
    rules = _wwn_rules()
    module = get_ruleset_module("wwn")
    res = module.seed_chargen_resources(rules=rules, stats={"INT": 14}, class_def=None)
    assert res.effort == {}
    assert res.spellcasting is None


def test_seed_chargen_resources_seeds_effort_and_spellcasting_for_magic_class() -> None:
    """Pin the INTERESTING case: a synthetic WWN magic class.

    Effort pool max = effort_base (1) + starting_skill_level (1) +
    swn_attribute_modifier(WIS). With WIS=14 the SWN curve gives +1, so the
    pool max is 1 + 1 + 1 = 3 (not partial, so no -1; floor of 1 does not
    apply). Spellcasting is seeded from the level-1 tables: casts_per_day=2,
    casts_remaining=2 (full at chargen), max_spell_level=1, and both starting
    prepared spells survive (no prepared_by_level cap → no truncation).

    Pinned values are UNCHANGED from the pre-migration Task-1 net — the
    migration is byte-identical (ADR-143).
    """
    rules = _wwn_rules()
    module = get_ruleset_module("wwn")
    # WWN standard_array → STR=14, DEX=12, CON=11, INT=10, WIS=9, CHA=7.
    # Override WIS to 14 so we exercise a non-zero attribute modifier (+1).
    stats = {"STR": 14, "DEX": 12, "CON": 11, "INT": 10, "WIS": 14, "CHA": 7}
    res = module.seed_chargen_resources(rules=rules, stats=stats, class_def=_high_mage_class())

    # Effort: one pool keyed by source, max pinned at 3.
    assert set(res.effort.keys()) == {"high_mage"}
    pool = res.effort["high_mage"]
    assert pool.source == "high_mage"
    assert pool.max == 3  # 1 (effort_base) + 1 (skill) + 1 (mod for WIS 14)
    assert pool.available == 3  # full at chargen
    assert pool.committed == 0

    # Spellcasting state pinned from the level-1 tables.
    sc = res.spellcasting
    assert sc is not None
    assert sc.casts_per_day == 2
    assert sc.casts_remaining == 2  # full at chargen
    assert sc.max_spell_level == 1
    assert sc.prepared == ["magic_dart", "ward"]


def test_seed_chargen_resources_floor_when_low_wisdom() -> None:
    """Pin the negative-modifier path: WIS=7 gives swn modifier -1.

    pool max = 1 (effort_base) + 1 (skill) + (-1) (mod) = 1. The SRD floor
    (a caster's Effort is always at least 1) leaves it at 1 here.
    """
    rules = _wwn_rules()
    module = get_ruleset_module("wwn")
    stats = {"STR": 14, "DEX": 12, "CON": 11, "INT": 10, "WIS": 7, "CHA": 7}
    res = module.seed_chargen_resources(rules=rules, stats=stats, class_def=_high_mage_class())

    assert res.effort["high_mage"].max == 1  # 1 + 1 + (-1) = 1
    assert res.spellcasting is not None
    assert res.spellcasting.casts_per_day == 2


def test_seed_chargen_resources_empty_for_non_wwn_rules() -> None:
    """A dial-ruleset RulesConfig must produce empty effort, None spellcasting."""
    native_rules = RulesConfig(
        stat_generation="standard_array",
        ability_score_names=list(WWN_ABILITY_NAMES),
        point_buy_budget=27,
    )
    module = get_ruleset_module("dial")
    res = module.seed_chargen_resources(rules=native_rules, stats={"INT": 14}, class_def=None)
    assert res.effort == {}
    assert res.spellcasting is None


# ---------------------------------------------------------------------------
# generate_stats — standard_array path pinned
# ---------------------------------------------------------------------------


def test_generate_stats_standard_array_fixed_order_wwn() -> None:
    """Pin CURRENT (pre-prime-aware) fixed-order assignment for the WWN array.

    The WWN standard_array [14, 12, 11, 10, 9, 7] maps to ability_score_names
    in declaration order: STR=14, DEX=12, CON=11, INT=10, WIS=9, CHA=7.
    No class_hint or stat_bonuses on AccumulatedChoices, so no derivation path
    fires — values are byte-identical to the pack-authored array.

    NOTE: The assertion on STR==14 (index-0) is expected to remain stable
    after ADR-143 extraction. The prime-aware change (Task 4) may reorder
    assignments for caster classes when a class_hint is present, but the
    no-class-hint path exercised here should be unaffected.
    """
    rules = _wwn_rules()
    builder = CharacterBuilder(
        scenes=_one_choice_scenes(),
        rules=rules,
        rng=random.Random(1),
    )
    acc = builder.accumulated()
    stats = builder.generate_stats(acc)

    # Pin the current fixed-order output (standard_array mapped in declaration order)
    assert stats["STR"] == 14  # index 0 of [14, 12, 11, 10, 9, 7]
    assert stats["DEX"] == 12  # index 1
    assert stats["CON"] == 11  # index 2
    assert stats["INT"] == 10  # index 3
    assert stats["WIS"] == 9  # index 4
    assert stats["CHA"] == 7  # index 5


def test_generate_stats_standard_array_full_dict() -> None:
    """Pin the complete stats dict for the WWN standard_array at seed 1."""
    rules = _wwn_rules()
    builder = CharacterBuilder(
        scenes=_one_choice_scenes(),
        rules=rules,
        rng=random.Random(1),
    )
    acc = builder.accumulated()
    stats = builder.generate_stats(acc)

    expected = {"STR": 14, "DEX": 12, "CON": 11, "INT": 10, "WIS": 9, "CHA": 7}
    assert stats == expected


def test_generate_stats_keys_match_ability_score_names() -> None:
    """Keys in generate_stats output match ability_score_names in all positions."""
    rules = _wwn_rules()
    builder = CharacterBuilder(scenes=_one_choice_scenes(), rules=rules)
    acc = builder.accumulated()
    stats = builder.generate_stats(acc)

    assert list(stats.keys()) == WWN_ABILITY_NAMES


def test_generate_stats_with_stat_bonuses_applied() -> None:
    """Stat bonuses from AccumulatedChoices stack on top of the array (all strategies)."""
    rules = _wwn_rules()
    builder = CharacterBuilder(scenes=_one_choice_scenes(), rules=rules)
    acc = builder.accumulated()
    acc.stat_bonuses["STR"] = 2
    acc.stat_bonuses["CHA"] = -1
    stats = builder.generate_stats(acc)

    assert stats["STR"] == 16  # 14 + 2
    assert stats["CHA"] == 6  # 7 - 1
    # Unbonused stats unchanged
    assert stats["DEX"] == 12
