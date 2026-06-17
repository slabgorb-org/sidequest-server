"""Story 126-5: the_story 'Appearance' input routes to Character.appearance,
never polluting background/backstory."""

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


def test_apply_story_routes_description_to_appearance_not_background():
    """Character.appearance holds the appearance text; background/backstory are clean."""
    b = _builder_parked_at_story()
    b.apply_response(
        StoryInput(
            pronouns="they/them",
            background="Former ratcatcher, owes the apothecary money.",
            description="Tall, soot-stained, missing a tooth.",
        )
    )
    # Auto-advance the_kit to reach Confirmation.
    b.apply_auto_advance()
    assert b.is_confirmation()

    char = b.build("Mara")

    # The 126-5 fix: description routes to appearance.
    assert char.appearance == "Tall, soot-stained, missing a tooth."

    # The 126-5 bug: appearance must NOT appear in background or backstory.
    assert "soot-stained" not in (char.background or "")
    assert "soot-stained" not in char.backstory

    # The typed background still flows to the background channel unchanged.
    assert "ratcatcher" in (b.accumulated().background or "")


def test_apply_story_empty_description_gives_empty_appearance():
    """When description is blank, Character.appearance defaults to empty string."""
    b = _builder_parked_at_story()
    b.apply_response(
        StoryInput(
            pronouns="she/her",
            background="Merchant's daughter.",
            description="   ",
        )
    )
    b.apply_auto_advance()
    assert b.is_confirmation()

    char = b.build("Lyra")

    assert char.appearance == ""
    # Background channel should only have the typed background.
    assert "Merchant" in (b.accumulated().background or "")
