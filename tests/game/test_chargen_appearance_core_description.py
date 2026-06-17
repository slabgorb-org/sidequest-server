"""OQ1 (Story 126-5): a typed appearance becomes the narrator-facing core.description."""

from __future__ import annotations

from sidequest.game.builder import (
    CharacterBuilder,
    StoryInput,
)
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    IdentityCapture,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

ABILITY_NAMES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


def _base_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=list(ABILITY_NAMES),
        default_class="Fighter",
        default_race="Human",
    )


def _make_scene(scene_id: str, **kwargs: object) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title=scene_id.replace("_", " ").title(),
        narration="...",
        **kwargs,  # type: ignore[arg-type]
    )


def _story_scene() -> CharCreationScene:
    """Synthetic the_story scene with identity_capture (pronouns required)."""
    return _make_scene(
        "the_story",
        choices=[],
        allows_freeform=True,
        mechanical_effects=MechanicalEffects(
            identity_capture=IdentityCapture(pronouns_required=True),
        ),
    )


def _builder_parked_at_story() -> CharacterBuilder:
    """Return a CharacterBuilder parked on the_story, ready for StoryInput."""
    scenes = [
        _make_scene(
            "the_calling",
            choices=[
                CharCreationChoice(
                    label="Fighter",
                    description="Strong of arm.",
                    mechanical_effects=MechanicalEffects(class_hint="Fighter"),
                ),
            ],
        ),
        _story_scene(),
        # the_kit is an auto-advance scene (no choices, no freeform).
        _make_scene("the_kit", choices=[], allows_freeform=False),
    ]
    b = CharacterBuilder(scenes=scenes, rules=_base_rules())
    # Advance past the_calling.
    b.apply_choice(0)
    assert b.current_scene().id == "the_story"
    return b


def test_typed_appearance_becomes_core_description():
    """OQ1: when the player typed an appearance, it becomes core.description."""
    b = _builder_parked_at_story()
    b.apply_response(
        StoryInput(
            pronouns="they/them",
            background="An ex-ratcatcher.",
            description="Tall, soot-stained, missing a tooth.",
        )
    )
    b.apply_auto_advance()
    assert b.is_confirmation()

    char = b.build("Mara")

    assert char.core.description == "Tall, soot-stained, missing a tooth."


def test_absent_appearance_falls_back_to_generic_description():
    """When appearance is blank, core.description is the generic 'A {race} {class}'."""
    b = _builder_parked_at_story()
    b.apply_response(
        StoryInput(
            pronouns="they/them",
            background="An ex-ratcatcher.",
            description="",
        )
    )
    b.apply_auto_advance()
    assert b.is_confirmation()

    char = b.build("Mara")

    # Generic "A {race} {class}" shape — non-blank, not the appearance.
    assert char.core.description
    assert "soot-stained" not in char.core.description
