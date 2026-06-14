"""WN-core standard-array assignment is prime-aware (ADR-143 Step 2)."""
from __future__ import annotations

from sidequest.game.builder import CharacterBuilder
from sidequest.game.ruleset import get_ruleset_module
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    ClassDef,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig


def _class(prime: str, display_name: str = "C") -> ClassDef:
    return ClassDef.model_validate({
        "id": "c",
        "display_name": display_name,
        "rpg_role": "control",
        "jungian_default": "magician",
        "prime_requisite": prime,
        "minimum_score": 9,
        "kit_table": "k",
    })


WWN_ABILITY_NAMES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


def _wwn_rules() -> RulesConfig:
    """Minimal WWN standard-array rules. The array's top value sits at index 0
    ([14, ...]) so prime placement onto a non-index-0 stat (INT) is observable."""
    return RulesConfig.model_validate(
        {
            "ruleset": "wwn",
            "stat_generation": "standard_array",
            "standard_array": [14, 12, 11, 10, 9, 7],
            "ability_score_names": WWN_ABILITY_NAMES,
            "wwn": {
                "attribute_map": {
                    "STRENGTH": "STR",
                    "DEXTERITY": "DEX",
                    "CONSTITUTION": "CON",
                    "INTELLIGENCE": "INT",
                    "WISDOM": "WIS",
                    "CHARISMA": "CHA",
                }
            },
        }
    )


def _one_choice_scenes() -> list[CharCreationScene]:
    return [
        CharCreationScene(
            id="pick",
            title="T",
            narration="N",
            choices=[
                CharCreationChoice(
                    label="Go",
                    description="desc",
                    mechanical_effects=MechanicalEffects(),
                )
            ],
        )
    ]


def test_caster_prime_gets_top_value():
    module = get_ruleset_module("wwn")
    names = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
    stats = module.assign_attributes(pool=[14, 12, 11, 10, 9, 7], ability_names=names, class_def=_class("INT"))
    assert stats["INT"] == 14  # prime lands the top value, NOT STR


def test_warrior_prime_gets_top_value():
    module = get_ruleset_module("wwn")
    names = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
    stats = module.assign_attributes(pool=[14, 12, 11, 10, 9, 7], ability_names=names, class_def=_class("STR"))
    assert stats["STR"] == 14


def test_no_class_def_falls_through_to_declaration_order():
    """With no class hint (class_def=None), prime=None → fill in declaration order."""
    module = get_ruleset_module("wwn")
    names = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
    pool = [14, 12, 11, 10, 9, 7]
    stats = module.assign_attributes(pool=pool, ability_names=names, class_def=None)
    # Declaration order: STR gets the highest pool value (index 0 after sort)
    assert stats["STR"] == 14
    assert stats["DEX"] == 12


def test_prime_not_in_ability_names_falls_through():
    """If prime is not in ability_names, fall through to high-to-low fill."""
    module = get_ruleset_module("wwn")
    names = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
    weird_class = _class("LUCK")  # not in ability_names
    stats = module.assign_attributes(pool=[14, 12, 11, 10, 9, 7], ability_names=names, class_def=weird_class)
    # STR should get top value since prime is unrecognized
    assert stats["STR"] == 14


def test_remaining_values_fill_high_to_low_by_declaration():
    """After prime gets the top value, remaining stats fill high-to-low in declaration order."""
    module = get_ruleset_module("wwn")
    names = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
    stats = module.assign_attributes(pool=[14, 12, 11, 10, 9, 7], ability_names=names, class_def=_class("INT"))
    assert stats["INT"] == 14   # prime gets top
    assert stats["STR"] == 12   # next in declaration order
    assert stats["DEX"] == 11
    assert stats["CON"] == 10
    assert stats["WIS"] == 9
    assert stats["CHA"] == 7


def test_all_values_from_pool_assigned():
    """All pool values appear exactly once in the result."""
    module = get_ruleset_module("wwn")
    names = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
    pool = [14, 12, 11, 10, 9, 7]
    stats = module.assign_attributes(pool=pool, ability_names=names, class_def=_class("WIS"))
    assert sorted(stats.values()) == sorted(pool)


def test_builder_generate_stats_wires_prime_aware_assignment():
    """WIRING: builder.generate_stats → WN ruleset.generate_attributes → WN
    assign_attributes is reachable from the real production path.

    A WN-bound CharacterBuilder with a class roster (INT-prime "High Mage") and
    an acc carrying that class as class_hint must place the array's top value
    (14) on INT — NOT on STR (index 0). This proves the builder resolves the
    class_def and threads it through to the prime-aware override, not just that
    the module method works in isolation."""
    rules = _wwn_rules()
    builder = CharacterBuilder(scenes=_one_choice_scenes(), rules=rules).with_classes(
        [_class("INT", display_name="High Mage")]
    )
    acc = builder.accumulated()
    acc.class_hint = "High Mage"  # matches the roster class display_name

    stats = builder.generate_stats(acc)

    # Top value lands on the prime (INT), proving the full chain wired:
    # generate_stats resolved class_def from class_hint + roster, passed it to
    # generate_attributes, which delegated to the WN prime-aware override.
    assert stats["INT"] == 14  # prime gets the top value
    assert stats["STR"] != 14  # NOT the declaration-order index-0 default
