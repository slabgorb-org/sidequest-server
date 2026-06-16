"""seed_chargen_resources seeds Effort pools + spellcasting state at chargen (ADR-143).

Mirrors test_builder_seeds_strain.py's idiom: synthetic RulesConfig +
synthetic WwnClassMagic, exercising the RulesetModule.seed_chargen_resources
surface directly, plus an end-to-end synthetic builder build proving the build()
attachment seam wires effort/spellcasting onto the CreatureCore exactly
as system_strain is.

Locked behaviors (pinned here so they can't silently drift):
- Level source: chargen level is always 1 (build() constructs level=1), so
  the by-level tables are read at key "1".
- Effort max per source: effort_base + starting_skill_level +
  swn_attribute_modifier(governing-attr score), with Partial -1 (floor 1).
- governing_attr is a CANONICAL key ("WISDOM") resolved via
  rules.wwn.attribute_map -> flavor name -> stats[flavor].
- casts_remaining seeds == casts_per_day (full at chargen); prepared == []
  (spells chosen at rest, Plan 3).
- Effort-only Art user (effort_sources but empty casts tables) =>
  spellcasting is None but the effort dict is still returned.
- Non-wwn ruleset OR class with no wwn_magic => empty effort, None — no half state.
"""

from __future__ import annotations

from sidequest.game.builder import CharacterBuilder
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.swn import swn_attribute_modifier
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    ClassDef,
    MechanicalEffects,
    WwnClassMagic,
    WwnEffortSource,
)
from sidequest.genre.models.rules import RulesConfig, WwnConfig

WWN_ABILITY_NAMES = ["Might", "Grace", "Vigor", "Wits", "Spirit", "Presence"]

WWN_ATTRIBUTE_MAP = {
    "STRENGTH": "Might",
    "DEXTERITY": "Grace",
    "CONSTITUTION": "Vigor",
    "INTELLIGENCE": "Wits",
    "WISDOM": "Spirit",
    "CHARISMA": "Presence",
}


def wwn_rules() -> RulesConfig:
    """Minimal valid wwn RulesConfig (effort_base default = 1)."""
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=list(WWN_ABILITY_NAMES),
        point_buy_budget=27,
        default_class="Mage",
        default_race="Human",
        ruleset="wwn",
        wwn=WwnConfig(attribute_map=WWN_ATTRIBUTE_MAP),
    )


def native_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class="Fighter",
        default_race="Human",
    )


def high_mage_def() -> ClassDef:
    """A full caster: one WISDOM-governed Effort source + cast tables."""
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
            casts_per_day_by_level={"1": 2, "2": 3},
            max_spell_level_by_level={"1": 1, "2": 1},
            prepared_by_level={"1": 3, "2": 4},
            partial=False,
        ),
    )


def vowed_def() -> ClassDef:
    """An Effort-only Art user (Vowed): effort source, NO spell economy, Partial."""
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
            partial=True,
        ),
    )


def fighter_def() -> ClassDef:
    """A non-magical class: wwn_magic is None."""
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
# Unit tests for seed_chargen_resources (migrated from seed_wwn_magic, ADR-143)
# ---------------------------------------------------------------------------

def _wwn_module():
    return get_ruleset_module("wwn")

def _native_module():
    return get_ruleset_module("native")


class TestSeedWwnMagicHelper:
    def test_full_caster_seeds_effort_pool_and_spellcasting(self) -> None:
        rules = wwn_rules()
        # Spirit (= WISDOM flavor) = 16 -> swn_attribute_modifier(16) == +1
        stats = {"Might": 10, "Grace": 12, "Vigor": 13, "Wits": 11, "Spirit": 16, "Presence": 9}
        cls = high_mage_def()

        res = _wwn_module().seed_chargen_resources(rules=rules, stats=stats, class_def=cls)

        # effort_base(1) + starting_skill_level(1) + mod(+1) == 3
        assert set(res.effort.keys()) == {"high_mage"}
        pool = res.effort["high_mage"]
        assert pool.source == "high_mage"
        assert pool.max == 1 + 1 + swn_attribute_modifier(16)
        assert pool.max == 3
        assert pool.commitments == []

        assert res.spellcasting is not None
        # Level 1 cast tables.
        assert res.spellcasting.casts_per_day == 2
        assert res.spellcasting.casts_remaining == 2  # full at chargen
        assert res.spellcasting.max_spell_level == 1
        assert res.spellcasting.prepared == []  # chosen at rest (Plan 3)

    def test_partial_class_drops_effort_by_one_floor_at_one(self) -> None:
        rules = wwn_rules()
        # Spirit = 8 -> mod 0; base 1 + skill 1 + 0 == 2; Partial -> 1.
        stats = {"Might": 10, "Grace": 10, "Vigor": 10, "Wits": 10, "Spirit": 8, "Presence": 10}
        cls = vowed_def()

        res = _wwn_module().seed_chargen_resources(rules=rules, stats=stats, class_def=cls)

        assert set(res.effort.keys()) == {"vowed"}
        # 1 + 1 + 0 == 2, Partial -1 == 1.
        assert res.effort["vowed"].max == 1
        # Effort-only Art user: no spell economy.
        assert res.spellcasting is None

    def test_partial_floor_clamps_to_one_when_formula_would_be_below(self) -> None:
        rules = wwn_rules()
        # Spirit = 3 -> mod -2; base 1 + skill 0 + (-2) == -1; Partial -> max(1, -2) == 1.
        stats = {"Might": 10, "Grace": 10, "Vigor": 10, "Wits": 10, "Spirit": 3, "Presence": 10}
        cls = vowed_def()
        cls.wwn_magic.effort_sources[0].starting_skill_level = 0  # type: ignore[union-attr]

        res = _wwn_module().seed_chargen_resources(rules=rules, stats=stats, class_def=cls)
        assert res.effort["vowed"].max == 1

    def test_non_partial_low_stat_still_floors_at_one(self) -> None:
        """Unconditional floor: a NON-partial caster whose formula is below 1
        (Spirit 3 -> mod -2, base 1 + skill 0 - 2 == -1) still clamps to 1.
        Locks the WWN-SRD min-1 rule outside the partial branch."""
        rules = wwn_rules()
        stats = {"Might": 10, "Grace": 10, "Vigor": 10, "Wits": 10, "Spirit": 3, "Presence": 10}
        cls = high_mage_def()
        assert cls.wwn_magic.partial is False  # type: ignore[union-attr]
        cls.wwn_magic.effort_sources[0].starting_skill_level = 0  # type: ignore[union-attr]

        res = _wwn_module().seed_chargen_resources(rules=rules, stats=stats, class_def=cls)
        assert res.effort["high_mage"].max == 1

    def test_multiple_effort_sources_each_seed_their_own_pool(self) -> None:
        """A dual-source caster (e.g. Elementalist + High Mage Art) seeds one
        pool per source, each with its own governing-attr/skill computation."""
        rules = wwn_rules()
        # Spirit (WISDOM) = 16 -> +1; Wits (INTELLIGENCE) = 14 -> +1.
        stats = {"Might": 10, "Grace": 10, "Vigor": 10, "Wits": 14, "Spirit": 16, "Presence": 10}
        cls = ClassDef(
            id="elementalist",
            display_name="Elementalist",
            rpg_role="caster",
            jungian_default="Magician",
            prime_requisite="WIS",
            minimum_score=9,
            kit_table="elementalist_kit",
            wwn_magic=WwnClassMagic(
                effort_sources=[
                    WwnEffortSource(
                        source="elemental",
                        governing_attr="INTELLIGENCE",
                        relevant_skill="Magic",
                        starting_skill_level=2,
                    ),
                    WwnEffortSource(
                        source="high_mage",
                        governing_attr="WISDOM",
                        relevant_skill="Magic",
                        starting_skill_level=1,
                    ),
                ],
                casts_per_day_by_level={"1": 2},
                max_spell_level_by_level={"1": 1},
                prepared_by_level={"1": 3},
                partial=False,
            ),
        )

        res = _wwn_module().seed_chargen_resources(rules=rules, stats=stats, class_def=cls)

        assert set(res.effort.keys()) == {"elemental", "high_mage"}
        # elemental: base 1 + skill 2 + mod(Wits=14 -> +1) == 4
        assert res.effort["elemental"].max == 1 + 2 + swn_attribute_modifier(14)
        assert res.effort["elemental"].max == 4
        # high_mage: base 1 + skill 1 + mod(Spirit=16 -> +1) == 3
        assert res.effort["high_mage"].max == 1 + 1 + swn_attribute_modifier(16)
        assert res.effort["high_mage"].max == 3
        assert res.spellcasting is not None
        assert res.spellcasting.casts_per_day == 2

    def test_governing_attr_resolves_canonical_to_flavor_to_score(self) -> None:
        """WISDOM -> attribute_map['WISDOM'] == 'Spirit' -> stats['Spirit']."""
        rules = wwn_rules()
        # Spirit = 18 -> mod +2; base 1 + skill 1 + 2 == 4.
        stats = {"Might": 8, "Grace": 8, "Vigor": 8, "Wits": 8, "Spirit": 18, "Presence": 8}
        cls = high_mage_def()

        res = _wwn_module().seed_chargen_resources(rules=rules, stats=stats, class_def=cls)
        assert res.effort["high_mage"].max == 1 + 1 + 2

    def test_non_wwn_ruleset_returns_empty_and_none(self) -> None:
        rules = native_rules()
        stats = {"STR": 10, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10}
        res = _native_module().seed_chargen_resources(rules=rules, stats=stats, class_def=fighter_def())
        assert res.effort == {}
        assert res.spellcasting is None

    def test_non_magic_class_returns_empty_and_none(self) -> None:
        rules = wwn_rules()
        stats = {"Might": 10, "Grace": 10, "Vigor": 10, "Wits": 10, "Spirit": 10, "Presence": 10}
        res = _wwn_module().seed_chargen_resources(rules=rules, stats=stats, class_def=fighter_def())
        assert res.effort == {}
        assert res.spellcasting is None

    def test_none_class_def_returns_empty_and_none(self) -> None:
        rules = wwn_rules()
        stats = {"Might": 10, "Grace": 10, "Vigor": 10, "Wits": 10, "Spirit": 10, "Presence": 10}
        res = _wwn_module().seed_chargen_resources(rules=rules, stats=stats, class_def=None)
        assert res.effort == {}
        assert res.spellcasting is None


# ---------------------------------------------------------------------------
# Integration: synthetic builder build wires effort/spellcasting onto the core
# ---------------------------------------------------------------------------


def make_scene(scene_id: str, choices: list[CharCreationChoice]) -> CharCreationScene:
    return CharCreationScene(id=scene_id, title="T", narration="N", choices=choices)


def make_choice(label: str, **fx: object) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description="desc",
        mechanical_effects=MechanicalEffects(**fx),  # type: ignore[arg-type]
    )


def test_wwn_full_caster_build_attaches_effort_and_spellcasting() -> None:
    """End-to-end synthetic builder: wwn pack + High Mage class def ->
    core.effort populated, core.spellcasting seeded, mirroring the
    system_strain attachment seam."""
    rules = wwn_rules()
    scenes = [
        make_scene(
            "crucible",
            choices=[make_choice("The Tower", class_hint="High Mage", race_hint="Human")],
        ),
    ]
    builder = CharacterBuilder(scenes=scenes, rules=rules).with_classes([high_mage_def()])
    builder.apply_choice(0)
    character = builder.build("Aludra Sael")

    # standard_array [15,14,13,12,10,8] -> [Might,Grace,Vigor,Wits,Spirit,Presence]
    spirit = character.stats["Spirit"]
    assert spirit == 10  # index-4 of standard_array -> mod 0

    assert "high_mage" in character.core.effort
    pool = character.core.effort["high_mage"]
    assert pool.max == 1 + 1 + swn_attribute_modifier(spirit)
    assert pool.max == 2

    assert character.core.spellcasting is not None
    assert character.core.spellcasting.casts_per_day == 2
    assert character.core.spellcasting.casts_remaining == 2
    assert character.core.spellcasting.max_spell_level == 1
    assert character.core.spellcasting.prepared == []


def test_wwn_non_magic_class_build_has_empty_effort_and_no_spellcasting() -> None:
    rules = wwn_rules()
    scenes = [
        make_scene(
            "crucible",
            choices=[make_choice("The Field", class_hint="Fighter", race_hint="Human")],
        ),
    ]
    builder = CharacterBuilder(scenes=scenes, rules=rules).with_classes([fighter_def()])
    builder.apply_choice(0)
    character = builder.build("Brom Hale")

    assert character.core.effort == {}
    assert character.core.spellcasting is None


def test_non_wwn_build_has_empty_effort_and_no_spellcasting() -> None:
    rules = native_rules()
    scenes = [make_scene("noop", choices=[make_choice("Go")])]
    builder = CharacterBuilder(scenes=scenes, rules=rules)
    builder.apply_choice(0)
    character = builder.build("Arven Steel")

    assert character.core.effort == {}
    assert character.core.spellcasting is None
