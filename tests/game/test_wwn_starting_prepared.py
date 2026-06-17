"""seed_chargen_resources seeds SpellcastingState.prepared from class starting_prepared (ADR-143).

Locked behaviors:
- ``starting_prepared`` on WwnClassMagic seeds SpellcastingState.prepared at chargen.
- Over-capacity (starting_prepared > prepared_by_level["1"]) is silently truncated.
- Absent capacity key (no "1" in prepared_by_level) → no truncation.
- Non-caster (effort-only, no casts tables) → spellcasting is None, no crash.
- Non-magic class (wwn_magic=None) OR non-wwn ruleset → empty effort, None.
"""

from __future__ import annotations

from sidequest.game.ruleset import get_ruleset_module
from sidequest.genre.models.character import (
    ClassDef,
    WwnClassMagic,
    WwnEffortSource,
)
from sidequest.genre.models.rules import RulesConfig, WwnConfig

# ---------------------------------------------------------------------------
# Shared fixtures (mirror test_wwn_chargen_seed.py idiom)
# ---------------------------------------------------------------------------

WWN_ABILITY_NAMES = ["Might", "Grace", "Vigor", "Wits", "Spirit", "Presence"]

WWN_ATTRIBUTE_MAP = {
    "STRENGTH": "Might",
    "DEXTERITY": "Grace",
    "CONSTITUTION": "Vigor",
    "INTELLIGENCE": "Wits",
    "WISDOM": "Spirit",
    "CHARISMA": "Presence",
}

_DEFAULT_STATS = {
    "Might": 10,
    "Grace": 10,
    "Vigor": 10,
    "Wits": 10,
    "Spirit": 10,
    "Presence": 10,
}


def _wwn_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=list(WWN_ABILITY_NAMES),
        point_buy_budget=27,
        default_class="Mage",
        default_race="Human",
        ruleset="wwn",
        wwn=WwnConfig(attribute_map=WWN_ATTRIBUTE_MAP),
    )


def _native_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class="Fighter",
        default_race="Human",
    )


def _caster_def(
    starting_prepared: list[str],
    prepared_by_level: dict[str, int],
    casts_per_day_by_level: dict[str, int] | None = None,
) -> ClassDef:
    """Build a minimal full-caster ClassDef with configurable starting_prepared."""
    return ClassDef(
        id="high_mage",
        display_name="High Mage",
        rpg_role="caster",
        jungian_default="Magician",
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
            casts_per_day_by_level=casts_per_day_by_level
            if casts_per_day_by_level is not None
            else {"1": 2},
            max_spell_level_by_level={"1": 1},
            prepared_by_level=prepared_by_level,
            starting_prepared=starting_prepared,
            partial=False,
        ),
    )


def _effort_only_def(starting_prepared: list[str] | None = None) -> ClassDef:
    """An Effort-only Art user (Vowed): effort source, NO spell economy."""
    return ClassDef(
        id="vowed",
        display_name="Vowed",
        rpg_role="warrior",
        jungian_default="Warrior",
        prime_requisite="WIS",
        minimum_score=9,
        kit_table="vowed_kit",
        wwn_magic=WwnClassMagic(
            effort_sources=[
                WwnEffortSource(
                    source="vowed",
                    governing_attr="WISDOM",
                    relevant_skill="Magic",
                    starting_skill_level=1,
                ),
            ],
            casts_per_day_by_level={},  # no spell economy
            max_spell_level_by_level={},
            prepared_by_level={},
            starting_prepared=starting_prepared or [],
            partial=True,
        ),
    )


def _fighter_def() -> ClassDef:
    return ClassDef(
        id="fighter",
        display_name="Fighter",
        rpg_role="warrior",
        jungian_default="Warrior",
        prime_requisite="STR",
        minimum_score=9,
        kit_table="fighter_kit",
        wwn_magic=None,
    )


# ---------------------------------------------------------------------------
# Tests — starting_prepared seeding (via seed_chargen_resources, ADR-143)
# ---------------------------------------------------------------------------

_wwn_mod = get_ruleset_module("wwn")
_dial_mod = get_ruleset_module("dial")


class TestStartingPreparedSeeding:
    def test_prepared_seeded_from_starting_prepared_at_capacity(self) -> None:
        """starting_prepared=[a,b] with prepared_by_level={"1":2} → prepared == ["a","b"]."""
        rules = _wwn_rules()
        cls = _caster_def(
            starting_prepared=["cinder_lance", "still_the_breath"],
            prepared_by_level={"1": 2},
        )

        res = _wwn_mod.seed_chargen_resources(rules=rules, stats=_DEFAULT_STATS, class_def=cls)

        assert res.spellcasting is not None
        assert res.spellcasting.prepared == ["cinder_lance", "still_the_breath"]

    def test_over_capacity_truncated_silently(self) -> None:
        """starting_prepared=[a,b,c] with prepared_by_level={"1":2} → prepared == ["a","b"]."""
        rules = _wwn_rules()
        cls = _caster_def(
            starting_prepared=["cinder_lance", "still_the_breath", "river_step"],
            prepared_by_level={"1": 2},
        )

        res = _wwn_mod.seed_chargen_resources(rules=rules, stats=_DEFAULT_STATS, class_def=cls)

        assert res.spellcasting is not None
        assert res.spellcasting.prepared == ["cinder_lance", "still_the_breath"]

    def test_absent_capacity_key_no_truncation(self) -> None:
        """starting_prepared=[a] with prepared_by_level={} → prepared == ["a"] (no truncation)."""
        rules = _wwn_rules()
        cls = _caster_def(
            starting_prepared=["cinder_lance"],
            prepared_by_level={},  # no "1" key
        )

        res = _wwn_mod.seed_chargen_resources(rules=rules, stats=_DEFAULT_STATS, class_def=cls)

        assert res.spellcasting is not None
        assert res.spellcasting.prepared == ["cinder_lance"]

    def test_empty_starting_prepared_yields_empty_list(self) -> None:
        """Default (no starting_prepared) → prepared == [] — existing behaviour preserved."""
        rules = _wwn_rules()
        cls = _caster_def(
            starting_prepared=[],
            prepared_by_level={"1": 3},
        )

        res = _wwn_mod.seed_chargen_resources(rules=rules, stats=_DEFAULT_STATS, class_def=cls)

        assert res.spellcasting is not None
        assert res.spellcasting.prepared == []

    def test_effort_only_class_spellcasting_is_none_no_crash(self) -> None:
        """An effort-only Art user (no cast tables) → spellcasting is None; no crash."""
        rules = _wwn_rules()
        cls = _effort_only_def(starting_prepared=["cinder_lance"])

        res = _wwn_mod.seed_chargen_resources(rules=rules, stats=_DEFAULT_STATS, class_def=cls)

        assert res.spellcasting is None
        # effort dict is still populated for the Vowed
        assert "vowed" in res.effort

    def test_no_wwn_magic_class_returns_empty_and_none(self) -> None:
        """Class with wwn_magic=None → empty effort, None spellcasting."""
        rules = _wwn_rules()
        res = _wwn_mod.seed_chargen_resources(
            rules=rules, stats=_DEFAULT_STATS, class_def=_fighter_def()
        )
        assert res.effort == {}
        assert res.spellcasting is None

    def test_non_wwn_ruleset_returns_empty_and_none(self) -> None:
        """Non-wwn ruleset → empty effort, None spellcasting."""
        rules = _native_rules()
        res = _dial_mod.seed_chargen_resources(
            rules=rules,
            stats={"STR": 10, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
            class_def=_fighter_def(),
        )
        assert res.effort == {}
        assert res.spellcasting is None
