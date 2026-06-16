"""Tests for MechanicalEffects.skill_grants / focus_id + AccumulatedChoices accumulation.

Task 8, ADR-143 (Ruleset Chargen Seam).

Verifies:
- skill_grants accumulate with higher-of (max) semantics, NOT additively
- focus_id accumulates onto acc.foci, de-duplicated
- Multiple scenes combine correctly
- Default construction still works (no new required fields)
"""

from __future__ import annotations

from sidequest.game.builder import (
    AccumulatedChoices,
    CharacterBuilder,
    ChoiceInput,
    SceneResult,
)
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

# ---------------------------------------------------------------------------
# Fixture helpers (mirrors test_builder_stats.py pattern)
# ---------------------------------------------------------------------------


def make_choice(label: str, description: str = "desc", **fields: object) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description=description,
        mechanical_effects=MechanicalEffects(**fields),  # type: ignore[arg-type]
    )


def make_scene(
    scene_id: str,
    *,
    choices: list[CharCreationChoice] | None = None,
    mechanical_effects: MechanicalEffects | None = None,
    title: str = "T",
    narration: str = "N",
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title=title,
        narration=narration,
        choices=choices or [],
        mechanical_effects=mechanical_effects,
    )


def rules_config() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class="Expert",
    )


def _inject_results(builder: CharacterBuilder, *effects: MechanicalEffects) -> None:
    """Directly push SceneResult entries into the builder for unit-testing accumulated()."""
    for i, eff in enumerate(effects):
        builder._results.append(
            SceneResult(
                input_type=ChoiceInput(index=0),
                effects_applied=eff,
                scene_index=i,
            )
        )


# ---------------------------------------------------------------------------
# MechanicalEffects — new fields exist and default cleanly
# ---------------------------------------------------------------------------


def test_mechanical_effects_skill_grants_defaults_empty() -> None:
    """skill_grants defaults to empty dict — existing construction unaffected."""
    eff = MechanicalEffects()
    assert eff.skill_grants == {}


def test_mechanical_effects_focus_id_defaults_none() -> None:
    """focus_id defaults to None — existing construction unaffected."""
    eff = MechanicalEffects()
    assert eff.focus_id is None


def test_mechanical_effects_skill_grants_roundtrip() -> None:
    """skill_grants survives construction."""
    eff = MechanicalEffects(skill_grants={"Sneak": 1, "Work": 0})
    assert eff.skill_grants == {"Sneak": 1, "Work": 0}


def test_mechanical_effects_focus_id_roundtrip() -> None:
    """focus_id survives construction."""
    eff = MechanicalEffects(focus_id="assassin")
    assert eff.focus_id == "assassin"


# ---------------------------------------------------------------------------
# AccumulatedChoices — new fields exist and default cleanly
# ---------------------------------------------------------------------------


def test_accumulated_choices_skill_grants_defaults_empty() -> None:
    acc = AccumulatedChoices()
    assert acc.skill_grants == {}


def test_accumulated_choices_foci_defaults_empty() -> None:
    acc = AccumulatedChoices()
    assert acc.foci == []


# ---------------------------------------------------------------------------
# accumulated() — skill_grants uses higher-of (max) semantics
# ---------------------------------------------------------------------------


def test_skill_grants_single_scene() -> None:
    """One scene granting skills appears in accumulated."""
    scenes = [
        make_scene("s1", choices=[make_choice("Thief", skill_grants={"Sneak": 1, "Work": 0})]),
    ]
    builder = CharacterBuilder(scenes=scenes, rules=rules_config())
    _inject_results(builder, MechanicalEffects(skill_grants={"Sneak": 1, "Work": 0}))
    acc = builder.accumulated()
    assert acc.skill_grants == {"Sneak": 1, "Work": 0}


def test_skill_grants_higher_of_wins_not_additive() -> None:
    """A later, lower grant does not raise (or lower) the skill — higher wins.

    Load-bearing WWN semantic: skill levels do NOT stack additively.
    Scene A grants Sneak-2, Scene B grants Sneak-1.
    Expected result: Sneak == 2 (max). Additive would give 3.
    """
    builder = CharacterBuilder(
        scenes=[make_scene("s1"), make_scene("s2")],
        rules=rules_config(),
    )
    _inject_results(
        builder,
        MechanicalEffects(skill_grants={"Sneak": 2}),
        MechanicalEffects(skill_grants={"Sneak": 1}),
    )
    acc = builder.accumulated()
    # max(2, 1) == 2, NOT 2+1=3
    assert acc.skill_grants["Sneak"] == 2, (
        f"Expected 2 (higher-of), got {acc.skill_grants['Sneak']} "
        "(additive would be 3 — WWN skills must not stack)"
    )


def test_skill_grants_higher_of_semantics_second_higher() -> None:
    """Explicit test that higher-of picks the SECOND grant when it's larger.

    Scene A: Sneak=0, Scene B: Sneak=2.
    Additive would give 2, higher-of also gives 2 — but the value of this
    test is Scene B: Work=2, Scene A: Work=1 below, where additive=3 != max=2.
    """
    builder = CharacterBuilder(
        scenes=[make_scene("s1"), make_scene("s2")],
        rules=rules_config(),
    )
    _inject_results(
        builder,
        MechanicalEffects(skill_grants={"Work": 1}),
        MechanicalEffects(skill_grants={"Work": 2}),
    )
    acc = builder.accumulated()
    # max(1, 2) == 2, NOT 1+2=3
    assert acc.skill_grants["Work"] == 2, (
        f"Expected 2 (higher-of), got {acc.skill_grants['Work']} "
        "(additive would be 3 — WWN skills must not stack)"
    )


def test_skill_grants_different_skills_merge() -> None:
    """Skills from different scenes all appear in the accumulated dict."""
    builder = CharacterBuilder(
        scenes=[make_scene("s1"), make_scene("s2")],
        rules=rules_config(),
    )
    _inject_results(
        builder,
        MechanicalEffects(skill_grants={"Sneak": 1}),
        MechanicalEffects(skill_grants={"Work": 1}),
    )
    acc = builder.accumulated()
    assert acc.skill_grants == {"Sneak": 1, "Work": 1}


# ---------------------------------------------------------------------------
# accumulated() — focus_id appends to foci, de-duplicated
# ---------------------------------------------------------------------------


def test_focus_id_single_scene() -> None:
    """One scene granting a focus appears in acc.foci."""
    builder = CharacterBuilder(
        scenes=[make_scene("s1")],
        rules=rules_config(),
    )
    _inject_results(builder, MechanicalEffects(focus_id="assassin"))
    acc = builder.accumulated()
    assert acc.foci == ["assassin"]


def test_focus_id_multiple_different() -> None:
    """Two distinct focus_ids both appear in acc.foci."""
    builder = CharacterBuilder(
        scenes=[make_scene("s1"), make_scene("s2")],
        rules=rules_config(),
    )
    _inject_results(
        builder,
        MechanicalEffects(focus_id="assassin"),
        MechanicalEffects(focus_id="warrior"),
    )
    acc = builder.accumulated()
    assert "assassin" in acc.foci
    assert "warrior" in acc.foci
    assert len(acc.foci) == 2


def test_focus_id_deduped() -> None:
    """Same focus_id from two scenes appears only once in acc.foci."""
    builder = CharacterBuilder(
        scenes=[make_scene("s1"), make_scene("s2")],
        rules=rules_config(),
    )
    _inject_results(
        builder,
        MechanicalEffects(focus_id="assassin"),
        MechanicalEffects(focus_id="assassin"),
    )
    acc = builder.accumulated()
    assert acc.foci == ["assassin"]


def test_focus_id_none_not_added() -> None:
    """Scenes without focus_id do not contribute to acc.foci."""
    builder = CharacterBuilder(
        scenes=[make_scene("s1")],
        rules=rules_config(),
    )
    _inject_results(builder, MechanicalEffects(skill_grants={"Sneak": 1}))
    acc = builder.accumulated()
    assert acc.foci == []


# ---------------------------------------------------------------------------
# Combined scenario: two skill scenes + one focus scene
# ---------------------------------------------------------------------------


def test_combined_skill_grants_and_foci() -> None:
    """Full scenario: two skill-granting scenes + one focus scene.

    Scene A: skill_grants={"Sneak": 1, "Work": 0}
    Scene B: skill_grants={"Sneak": 0, "Work": 1}  ← lower Sneak, higher Work
    Scene C: focus_id="assassin"

    Expected:
      skill_grants == {"Sneak": 1, "Work": 1}  (max of each)
      foci == ["assassin"]
    """
    builder = CharacterBuilder(
        scenes=[make_scene("s1"), make_scene("s2"), make_scene("s3")],
        rules=rules_config(),
    )
    _inject_results(
        builder,
        MechanicalEffects(skill_grants={"Sneak": 1, "Work": 0}),
        MechanicalEffects(skill_grants={"Sneak": 0, "Work": 1}),
        MechanicalEffects(focus_id="assassin"),
    )
    acc = builder.accumulated()
    assert acc.skill_grants == {"Sneak": 1, "Work": 1}, (
        f"Expected max of each skill across scenes, got {acc.skill_grants}"
    )
    assert acc.foci == ["assassin"]
