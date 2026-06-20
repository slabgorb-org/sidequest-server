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


class TestArticleLeadingCannedLabel:
    """sq-playtest 2026-06-10 BUG-LOW: heavy_metal's calling scene uses oblique
    flavor labels ("A craft that costs the craftsman") with the real class in
    class_hint. Capturing the label verbatim produced the doubled-article
    Calling ("Vesska, a A craft that costs the craftsman"). A label leading with
    an article is NOT a vocation title — leave class_label empty so build-time
    falls back to the resolved class_hint. Confirmed genre-wide (spaghetti_western
    "The Gun" → Gunslinger, wry_whimsy "A curious child" → "Curious Child")."""

    def _calling_scene(self) -> CharCreationScene:
        # heavy_metal/long_foundry "obligation" calling scene shape.
        return make_scene(
            "calling",
            choices=[
                make_choice("A craft that costs the craftsman", class_hint="Elementalist"),
                make_choice("The Gun", class_hint="Gunslinger"),
            ],
            allows_freeform=True,
        )

    def test_article_leading_label_is_not_captured_as_vocation(self) -> None:
        b = CharacterBuilder(scenes=[self._calling_scene()], rules=simple_rules())
        b.apply_choice(0)
        acc = b.accumulated()
        # Mechanical class still resolves...
        assert acc.class_hint == "Elementalist"
        # ...but the oblique flavor phrase is NOT stamped as the display label.
        assert acc.class_label is None
        # {class} prose falls back to the resolved class — no doubled article, and
        # the hardcoded indefinite article now AGREES with the value (sq-playtest
        # 150-6): "a {class}" before a vowel-initial vocation renders "an
        # Elementalist", not the ungrammatical "a Elementalist".
        assert b.interpolate_scene_narration("a {class}") == "an Elementalist"

    def test_definite_article_label_also_skipped(self) -> None:
        b = CharacterBuilder(scenes=[self._calling_scene()], rules=simple_rules())
        b.apply_choice(1)
        acc = b.accumulated()
        assert acc.class_hint == "Gunslinger"
        assert acc.class_label is None

    def test_non_article_vocation_label_still_captured(self) -> None:
        # Regression guard: the tea_and_murder path ("Country Doctor") must keep
        # capturing its flavor label — the fix only skips article-leading labels.
        b = CharacterBuilder(scenes=[_vocation_scene()], rules=simple_rules())
        b.apply_choice(0)
        acc = b.accumulated()
        assert acc.class_hint == "Doctor"
        assert acc.class_label == "Country Doctor"


class TestIndefiniteArticleOriginLabel:
    """sq-playtest 2026-06-10 BUG-LOW (barsoom Tarkas): the race-axis sibling of
    the calling-axis fix above. barsoom's origin labels are indefinite-article
    descriptor phrases ("A Green Martian of the Hordes") with the real race in
    race_hint ("Green Martian"); stamping the label as the origin display made
    the sheet read "Race: A Green Martian of the Hordes".

    The guard is INDEFINITE-only ("a"/"an") — a full-corpus survey shows every
    a/an origin label reads better as its race_hint ("A Sealed Vault" → Pure
    Strain Human, "A Lab" → Synthetic, all five barsoom origins), while
    definite-article labels are intended displays everywhere ("The Village
    Itself" over Servant, "The Streets" over Street, the elemental_harmony
    "The …" homelands). Do NOT widen this guard to "the"."""

    def _origin_scene(self) -> CharCreationScene:
        return make_scene(
            "origin",
            choices=[
                make_choice("A Green Martian of the Hordes", race_hint="Green Martian"),
                make_choice("The Village Itself", race_hint="Servant"),
            ],
        )

    def test_indefinite_article_origin_label_not_captured(self) -> None:
        b = CharacterBuilder(scenes=[self._origin_scene()], rules=simple_rules())
        b.apply_choice(0)
        acc = b.accumulated()
        # Mechanical race still resolves...
        assert acc.race_hint == "Green Martian"
        # ...but the descriptor phrase is NOT stamped as the origin display —
        # the sheet falls back to the resolved race.
        assert acc.race_label is None

    def test_definite_article_origin_label_still_captured(self) -> None:
        # tea_and_murder "The Village Itself" (race_hint Servant) is the
        # documented reason race_label exists — it must keep its display.
        b = CharacterBuilder(scenes=[self._origin_scene()], rules=simple_rules())
        b.apply_choice(1)
        acc = b.accumulated()
        assert acc.race_hint == "Servant"
        assert acc.race_label == "The Village Itself"
