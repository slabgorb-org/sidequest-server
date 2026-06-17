"""standard_array_arrange seeds the arrange pool from the standard array (ADR-143)."""

from __future__ import annotations

import random

from sidequest.game.builder import CharacterBuilder, FreeformInput
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    IdentityCapture,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig


def _arrange_scenes() -> list[CharCreationScene]:
    """Minimal scene list whose first scene seeds the standard_array_arrange pool."""
    return [
        CharCreationScene(
            id="the_roll",
            title="Roll",
            narration="...",
            choices=[],
            allows_freeform=False,
            mechanical_effects=MechanicalEffects(
                stat_generation="standard_array_arrange",
            ),
        ),
        CharCreationScene(
            id="the_arrangement",
            title="Arrange",
            narration="...",
            choices=[],
            allows_freeform=False,
            mechanical_effects=MechanicalEffects(
                assignment_required=True,
                allow_reject=False,
            ),
        ),
        CharCreationScene(
            id="the_calling",
            title="Call",
            narration="...",
            choices=[
                CharCreationChoice(
                    label="Fighter",
                    description="Strong of arm.",
                    mechanical_effects=MechanicalEffects(class_hint="Fighter"),
                ),
            ],
            allows_freeform=False,
        ),
        CharCreationScene(
            id="the_story",
            title="Story",
            narration="...",
            choices=[],
            allows_freeform=True,
            mechanical_effects=MechanicalEffects(
                identity_capture=IdentityCapture(pronouns_required=True),
            ),
        ),
        CharCreationScene(
            id="the_kit",
            title="Kit",
            narration="...",
            choices=[],
            allows_freeform=False,
        ),
    ]


def _rules(array: list[int] | None = None) -> RulesConfig:
    return RulesConfig.model_validate(
        {
            "ruleset": "wwn",
            "stat_generation": "standard_array_arrange",
            "standard_array": array if array is not None else [14, 12, 11, 10, 9, 7],
            "ability_score_names": ["STR", "DEX", "CON", "INT", "WIS", "CHA"],
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


def test_pool_seeded_from_standard_array():
    """Pool values must exactly match the pack's standard_array (ADR-143 DD-3)."""
    rules = _rules([14, 12, 11, 10, 9, 7])
    builder = CharacterBuilder(scenes=_arrange_scenes(), rules=rules, rng=random.Random(1))
    assert sorted(builder.arrangement_pool()) == [7, 9, 10, 11, 12, 14]


def test_pool_uses_fallback_when_standard_array_unset():
    """When standard_array is None, the engine default [15,14,13,12,10,8] is used."""
    rules = RulesConfig.model_validate(
        {
            "ruleset": "wwn",
            "stat_generation": "standard_array_arrange",
            "ability_score_names": ["STR", "DEX", "CON", "INT", "WIS", "CHA"],
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
    builder = CharacterBuilder(scenes=_arrange_scenes(), rules=rules, rng=random.Random(1))
    assert sorted(builder.arrangement_pool()) == sorted([15, 14, 13, 12, 10, 8])


def test_assignment_initially_all_none():
    """All six stat slots start unassigned."""
    rules = _rules()
    builder = CharacterBuilder(scenes=_arrange_scenes(), rules=rules, rng=random.Random(1))
    assignment = builder.arrangement_assignment()
    assert assignment is not None
    assert set(assignment.keys()) == {"STR", "DEX", "CON", "INT", "WIS", "CHA"}
    assert all(v is None for v in assignment.values())


def test_pool_is_deterministic_and_rng_independent():
    """standard_array_arrange pool is fixed — two different RNG seeds produce identical pools."""
    rules = _rules([14, 12, 11, 10, 9, 7])
    a = CharacterBuilder(scenes=_arrange_scenes(), rules=rules, rng=random.Random(0))
    b = CharacterBuilder(scenes=_arrange_scenes(), rules=rules, rng=random.Random(99))
    assert a.arrangement_pool() == b.arrangement_pool()


def test_reject_reseeds_from_fixed_array_not_reroll():
    """reject_arrangement in standard_array_arrange mode re-seeds from the fixed
    standard array (NOT a 3d6 re-roll) and clears all assignments (ADR-143 DD-3).

    Guards against a future cleanup accidentally routing the standard_array_arrange
    reject path back through _roll_3d6_arrange_visible."""
    array = [14, 12, 11, 10, 9, 7]
    rules = _rules(array)
    builder = CharacterBuilder(scenes=_arrange_scenes(), rules=rules, rng=random.Random(1))
    # Assign a couple of stats so the pool is partially drained and the
    # assignment dict is non-trivial before the reject.
    builder.assign_stat("STR", 14)
    builder.assign_stat("DEX", 12)
    assert builder.arrangement_assignment()["STR"] == 14
    assert builder.arrangement_assignment()["DEX"] == 12

    builder.reject_arrangement()

    # (a) Pool values are IDENTICAL to the standard array — not re-rolled.
    assert sorted(builder.arrangement_pool()) == sorted(array)
    # (b) Assignment cleared to all-None.
    assignment = builder.arrangement_assignment()
    assert set(assignment.keys()) == {"STR", "DEX", "CON", "INT", "WIS", "CHA"}
    assert all(v is None for v in assignment.values())


def test_arrange_scene_renders_stat_arrange_input_type():
    """The arrangement scene (assignment_required=True) renders input_type='stat_arrange'
    with the seeded pool when stat_generation is standard_array_arrange (ADR-143 DD-3)."""
    rules = _rules([14, 12, 11, 10, 9, 7])
    builder = CharacterBuilder(scenes=_arrange_scenes(), rules=rules, rng=random.Random(1))
    # Advance past the seeding scene (index 0) to the arrangement scene (index 1).
    # the_roll has no choices; FreeformInput advances it per test_builder_arrangement_scene_flow.
    while builder.current_scene().id != "the_arrangement":
        builder.apply_response(FreeformInput(text=""))
    msg = builder.to_scene_message(player_id="p1")
    payload = msg.payload
    assert payload.input_type == "stat_arrange"
    assert payload.pool is not None
    assert sorted(payload.pool) == [7, 9, 10, 11, 12, 14]
