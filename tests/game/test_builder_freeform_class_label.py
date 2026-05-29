"""Free-text vocation produces a display label for the ``{class}`` prose slot.

Regression for the playtest bug where a custom (freeform) vocation left the
``{class}`` token empty — the tea_and_murder loadout prose
"the working contents of a {class}'s working life" rendered as "a 's working
life". A class-selecting scene's canned choices each carry ``class_hint``; the
freeform path carried none, so ``acc.class_hint`` stayed ``None`` and the slot
resolved empty.

The fix captures the player's freeform text as a display-only ``class_label``
that feeds ``{class}`` substitution, WITHOUT setting the mechanical
``class_hint`` (which must keep resolving to the pack default so the
starting-loadout class match in ``apply_starting_loadout`` does not regress).
"""

from __future__ import annotations

from sidequest.game.builder import CharacterBuilder, derive_class_label
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig


def make_choice(label: str, **effect_fields: object) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description="A description.",
        mechanical_effects=MechanicalEffects(**effect_fields),  # type: ignore[arg-type]
    )


def make_scene(
    scene_id: str,
    *,
    choices: list[CharCreationChoice] | None = None,
    allows_freeform: bool | None = None,
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title="Scene title",
        narration="Scene narration.",
        choices=choices or [],
        allows_freeform=allows_freeform,
    )


def simple_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class="Detective",
        default_race="Human",
    )


def _vocation_scene() -> CharCreationScene:
    """A class-selecting scene: every canned choice carries a class_hint, and
    the scene also allows freeform — matching tea_and_murder's `vocation`."""
    return make_scene(
        "vocation",
        choices=[
            make_choice("Country Doctor", class_hint="Doctor"),
            make_choice("Village Constable", class_hint="Detective"),
        ],
        allows_freeform=True,
    )


class TestDeriveClassLabel:
    def test_strips_leading_article_and_trailing_flavor(self) -> None:
        text = "A vegetarian and temperance lecturer — earnest, melancholy, forever ignored."
        assert derive_class_label(text) == "vegetarian and temperance lecturer"

    def test_period_terminates_label(self) -> None:
        assert derive_class_label("The village ratcatcher. He drinks too much.") == (
            "village ratcatcher"
        )

    def test_an_article_stripped(self) -> None:
        assert derive_class_label("an itinerant photographer") == "itinerant photographer"

    def test_plain_noun_passthrough(self) -> None:
        assert derive_class_label("blacksmith") == "blacksmith"

    def test_collapses_internal_whitespace(self) -> None:
        assert derive_class_label("  the   parish    organist  ") == "parish organist"


class TestFreeformVocationLabel:
    def test_freeform_vocation_fills_class_slot(self) -> None:
        b = CharacterBuilder(scenes=[_vocation_scene()], rules=simple_rules())
        b.apply_freeform(
            "A vegetarian and temperance lecturer — earnest, melancholy, forever ignored."
        )
        rendered = b.interpolate_scene_narration(
            "the working contents of a {class}'s working life."
        )
        assert rendered == (
            "the working contents of a vegetarian and temperance lecturer's working life."
        )

    def test_freeform_vocation_does_not_set_mechanical_class_hint(self) -> None:
        # Mechanical class must stay on the pack default so the
        # starting-loadout class match does not regress.
        b = CharacterBuilder(scenes=[_vocation_scene()], rules=simple_rules())
        b.apply_freeform("A vegetarian and temperance lecturer.")
        assert b.accumulated().class_hint is None
        assert b.accumulated().class_label == "vegetarian and temperance lecturer"

    def test_canned_choice_captures_flavor_label_but_keeps_mechanical_hint(self) -> None:
        # sq-playtest 2026-05-28 BUG-LOW: a canned choice now captures its
        # flavor LABEL ("Country Doctor") for display while the MECHANICAL
        # class_hint stays the collapsed archetype ("Doctor") so the
        # starting-loadout class match is unaffected. {class} prose prefers the
        # flavor label (Diamonds-and-Coal: surface what the player chose).
        b = CharacterBuilder(scenes=[_vocation_scene()], rules=simple_rules())
        b.apply_choice(0)
        acc = b.accumulated()
        assert acc.class_hint == "Doctor"  # mechanical archetype unchanged
        assert acc.class_label == "Country Doctor"  # display flavor captured
        assert b.interpolate_scene_narration("a {class}") == "a Country Doctor"

    def test_freeform_on_non_class_scene_sets_no_label(self) -> None:
        # A name-entry style scene (freeform, no class-bearing choices) must not
        # capture a class_label.
        b = CharacterBuilder(
            scenes=[make_scene("name", allows_freeform=True)],
            rules=simple_rules(),
        )
        b.apply_freeform("Neil")
        assert b.accumulated().class_label is None
