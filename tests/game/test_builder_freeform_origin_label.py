"""Free-text ORIGIN produces a display label for origin_label / background / race.

Regression for the sq-playtest 2026-06-16 bug: a free-text answer on the
origin/background chargen step (the "describe it in your own words" box on a
race-selecting scene) was DROPPED. A canned choice carries a ``race_hint`` +
``background``; the freeform path carried neither, so ``acc.race_label`` and
``acc.background_label`` stayed ``None`` — the built Character got
``origin_label=''``, ``background=''``, and (on a Fate pack, where race falls
back to a display label) ``race`` = the pack's default high-concept string
("Disbarred Lawyer Working the Other Side of the Law") — a value unrelated to
what the player typed.

The fix mirrors the freeform_class_label machinery on the race/origin axis:
``apply_freeform`` derives a display-only label from the text on a
race-selecting scene; ``accumulated()`` fills race_label (-> origin_label) and
background_label (-> Character.background); ``build()``'s Fate race fallback
prefers that origin label over the high-concept. No mechanical ``race_hint`` is
set — the open NL path persists as flavor only (Yes-And / the Zork problem),
with no mechanical advantage and no skill grant (DD-5).
"""

from __future__ import annotations

from sidequest.game.builder import CharacterBuilder
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

HIGH_CONCEPT = "Disbarred Lawyer Working the Other Side of the Law"
TROUBLE = "I Can't Leave a Loose Thread Alone"
NOIR_SKILLS = {"Investigate": 3, "Contacts": 2, "Deceive": 2, "Shoot": 1, "Notice": 1}


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
        title="Where'd You Come From?",
        narration="The bartender looks up.",
        choices=choices or [],
        allows_freeform=allows_freeform,
    )


def simple_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class="Detective",
        default_race="Street",
    )


def fate_rules() -> RulesConfig:
    """A minimal ``ruleset: fate`` RulesConfig shaped like pulp_noir, including
    the default high-concept the dropped-origin bug used to leak into race."""
    return RulesConfig.model_validate(
        {
            "ruleset": "fate",
            "fate": {
                "skills": dict(NOIR_SKILLS),
                "refresh": 3,
                "default_high_concept": HIGH_CONCEPT,
                "default_trouble": TROUBLE,
            },
            "stat_generation": "standard_array",
            "standard_array": [15, 14, 13, 12, 10, 8],
            "ability_score_names": ["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        }
    )


def _origin_scene() -> CharCreationScene:
    """A race/origin-selecting scene: every canned choice carries a race_hint +
    background, and the scene also allows freeform — matching pulp_noir's
    ``origins`` step."""
    return make_scene(
        "origins",
        choices=[
            make_choice("The Streets", race_hint="Street", background="Hustler"),
            make_choice("The Service", race_hint="Military", background="Veteran"),
        ],
        allows_freeform=True,
    )


FREE_TEXT = "A Spanish painter who came to Paris chasing the avant-garde"
DERIVED = "Spanish painter who came to Paris chasing the avant-garde"


class TestFreeformOriginAccumulation:
    def test_freeform_origin_fills_origin_and_background_labels(self) -> None:
        b = CharacterBuilder(scenes=[_origin_scene()], rules=simple_rules())
        b.apply_freeform(FREE_TEXT)
        acc = b.accumulated()
        # The article is stripped; both display slots are filled from the text.
        assert acc.race_label == DERIVED
        assert acc.background_label == DERIVED

    def test_freeform_origin_does_not_set_mechanical_race_hint(self) -> None:
        # Display-only — the mechanical race_hint stays unset (no advantage; the
        # freeform background grants no skills, DD-5).
        b = CharacterBuilder(scenes=[_origin_scene()], rules=simple_rules())
        b.apply_freeform(FREE_TEXT)
        assert b.accumulated().race_hint is None

    def test_preset_origin_choice_still_works(self) -> None:
        # Regression: the canned path is unchanged — race_hint set mechanically,
        # and the chosen label surfaces as origin display + background label.
        b = CharacterBuilder(scenes=[_origin_scene()], rules=simple_rules())
        b.apply_choice(1)  # "The Service"
        acc = b.accumulated()
        assert acc.race_hint == "Military"
        assert acc.race_label == "The Service"
        assert acc.background_label == "The Service"

    def test_freeform_on_non_race_scene_sets_no_origin_label(self) -> None:
        # A name-entry style scene (freeform, no race-bearing choices) must not
        # capture a race_label.
        b = CharacterBuilder(
            scenes=[make_scene("name", allows_freeform=True)],
            rules=simple_rules(),
        )
        b.apply_freeform("Picasso")
        assert b.accumulated().race_label is None


class TestFreeformOriginFateBuild:
    """End-to-end wiring: a Fate PC built through the freeform origin path must
    surface the typed origin — NOT the default high-concept — across race,
    origin_label, and background."""

    def test_freeform_origin_fate_build_uses_origin_not_high_concept(self) -> None:
        b = CharacterBuilder(scenes=[_origin_scene()], rules=fate_rules())
        b.apply_freeform(FREE_TEXT)
        character = b.build("Picasso")
        # The bug: race == HIGH_CONCEPT. The fix: race == the typed origin.
        assert character.race != HIGH_CONCEPT
        assert character.race == DERIVED
        assert character.origin_label == DERIVED
        assert character.background == DERIVED

    def test_preset_origin_fate_build_unaffected(self) -> None:
        # Regression: the preset path keeps race = mechanical race_hint and the
        # chosen label as origin_label/background — never the high-concept.
        b = CharacterBuilder(scenes=[_origin_scene()], rules=fate_rules())
        b.apply_choice(1)  # "The Service"
        character = b.build("Braque")
        assert character.race == "Military"
        assert character.origin_label == "The Service"
        assert character.background == "The Service"
        assert character.race != HIGH_CONCEPT
