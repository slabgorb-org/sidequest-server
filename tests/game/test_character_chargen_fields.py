"""Tests for canned-openings P2 plumbing — Character chargen-derived fields.

Verifies that ``CharacterBuilder.build`` populates ``Character.background``,
``Character.drive``, ``Character.first_name``, ``Character.last_name``, and
``Character.nickname`` end-to-end.

These fields are consumed by
``_populate_opening_directive_on_chargen_complete`` in
``sidequest/server/websocket_session_handler.py`` to filter Openings by
``triggers.backgrounds`` (which match LABELS, not mechanical tags) and to
render the chassis-voice block. Until this plumbing landed every
``getattr(pc, "background", "")`` returned ""; the entire background-keyed
Opening selection pipeline was dead.

Each test drives a real ``CharacterBuilder`` through ``apply_choice`` /
``apply_freeform`` / ``build`` rather than poking the ``Character``
constructor — that's the wiring this suite is meant to verify.
"""

from __future__ import annotations

from sidequest.game.builder import CharacterBuilder
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

ABILITY_NAMES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


# ---------------------------------------------------------------------------
# Fixture helpers — cloned from tests/game/test_builder_build.py pattern.
# ---------------------------------------------------------------------------


def make_choice(label: str, description: str = "desc", **fx: object) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description=description,
        mechanical_effects=MechanicalEffects(**fx),  # type: ignore[arg-type]
    )


def make_scene(
    scene_id: str,
    *,
    choices: list[CharCreationChoice] | None = None,
    allows_freeform: bool | None = None,
    mechanical_effects: MechanicalEffects | None = None,
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title="T",
        narration="N",
        choices=choices or [],
        allows_freeform=allows_freeform,
        mechanical_effects=mechanical_effects,
    )


def base_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=list(ABILITY_NAMES),
        point_buy_budget=27,
        default_class="Fighter",
        default_race="Human",
    )


# ---------------------------------------------------------------------------
# Case 1 — background label captured (Coyote Star "origins" pattern).
# ---------------------------------------------------------------------------


class TestBackgroundLabelCaptured:
    def test_background_field_is_label_not_mechanical_tag(self) -> None:
        """A scene with mechanical_effects.background = 'Far Landing-raised'
        and label = 'Far Landing Raised Me' should populate
        Character.background with the LABEL, because Validator 8 derives
        chargen_backgrounds from labels and Opening.triggers.backgrounds
        matches against that list.
        """
        scenes = [
            make_scene(
                "origins",
                choices=[
                    make_choice(
                        "Far Landing Raised Me",
                        description="The dust ports raised me.",
                        background="Far Landing-raised",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Zanzibar Vesh")
        # Stored value is the LABEL, not the mechanical tag.
        assert char.background == "Far Landing Raised Me"
        assert char.background != "Far Landing-raised"


# ---------------------------------------------------------------------------
# Case 2 — drive label captured (relationship/goals/emotional_state shape).
# ---------------------------------------------------------------------------


class TestDriveLabelCaptured:
    def test_drive_shaped_scene_populates_drive_field(self) -> None:
        """A scene whose effects touch the inner-life triplet
        (relationship / goals / emotional_state) WITHOUT race/class/
        mutation/rig hints is detected by builder.py's looks_like_drive
        check. Its choice label is captured to backstory_label and
        plumbed into Character.drive.
        """
        scenes = [
            make_scene(
                "drive",
                choices=[
                    make_choice(
                        "Someone Went Into the Drift",
                        description="My sister never came back from the Drift.",
                        relationship="missing sister",
                        goals="find her",
                        emotional_state="haunted",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Anon")
        assert char.drive == "Someone Went Into the Drift"


# ---------------------------------------------------------------------------
# Case 3 — name splitting (three sub-cases).
# ---------------------------------------------------------------------------


def _trivial_confirmation_builder() -> CharacterBuilder:
    """A builder one apply_choice away from confirmation — no narrative
    side effects, used only to exercise name splitting in build()."""
    scenes = [
        make_scene(
            "noop",
            choices=[make_choice("Go", description="A blank slate.")],
        ),
    ]
    b = CharacterBuilder(scenes=scenes, rules=base_rules())
    b.apply_choice(0)
    return b


class TestNameSplitting:
    def test_first_and_last(self) -> None:
        b = _trivial_confirmation_builder()
        char = b.build("Zanzibar Vesh")
        assert char.first_name == "Zanzibar"
        assert char.last_name == "Vesh"

    def test_single_token(self) -> None:
        b = _trivial_confirmation_builder()
        char = b.build("Zanzibar")
        assert char.first_name == "Zanzibar"
        assert char.last_name == ""

    def test_three_tokens_groups_remainder(self) -> None:
        b = _trivial_confirmation_builder()
        char = b.build("Mary Jane Doe")
        assert char.first_name == "Mary"
        assert char.last_name == "Jane Doe"


# ---------------------------------------------------------------------------
# Case 4 — empty defaults when no background/drive scene authored.
# ---------------------------------------------------------------------------


class TestEmptyDefaults:
    def test_no_background_or_drive_scene_leaves_fields_empty(self) -> None:
        """A chargen flow that doesn't set MechanicalEffects.background and
        has no drive-shaped scene should leave Character.background and
        Character.drive as empty strings — explicit absence, the value
        the helper expects when filtering Openings without a chargen
        signal.
        """
        scenes = [
            make_scene(
                "noop",
                choices=[make_choice("Go", description="A blank slate.")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Anon")
        assert char.background == ""
        assert char.drive == ""


# ---------------------------------------------------------------------------
# Case 4b — origin/calling flavor labels plumbed onto Character (#G2 live-panel
# extension). The collapsed mechanical race/class slug stays authoritative;
# the display-only labels carry the chosen flavor for the player-facing sheet.
# ---------------------------------------------------------------------------


class TestOriginCallingLabelsPlumbed:
    def _vocation_origin_scenes(self) -> list[CharCreationScene]:
        # tea_and_murder shape: a CHOICE whose label is a rich flavor phrase
        # that collapses onto a small mechanical archetype (race_hint/class_hint).
        return [
            make_scene(
                "origin",
                choices=[
                    make_choice(
                        "The Village Itself",
                        description="A crofter, of the working village.",
                        race_hint="Servant",
                    ),
                ],
            ),
            make_scene(
                "vocation",
                choices=[
                    make_choice(
                        "Country Veterinary Surgeon",
                        description="You tend the parish's beasts.",
                        class_hint="Doctor",
                    ),
                ],
            ),
        ]

    def test_choice_flavor_labels_populate_character_labels(self) -> None:
        b = CharacterBuilder(scenes=self._vocation_origin_scenes(), rules=base_rules())
        b.apply_choice(0)
        b.apply_choice(0)
        char = b.build("Vyvyan Basterd")
        # Display-only labels carry the chosen flavor.
        assert char.origin_label == "The Village Itself"
        assert char.calling_label == "Country Veterinary Surgeon"
        # Mechanical archetype underneath is unchanged — loadout/genre read these.
        assert char.race == "Servant"
        assert char.char_class == "Doctor"

    def test_labels_empty_when_no_distinct_flavor(self) -> None:
        # A choice whose label IS the archetype leaves the labels empty so the
        # UI falls back to the slug (label==hint → byte-identical surface).
        scenes = [
            make_scene(
                "noop",
                choices=[make_choice("Go", description="A blank slate.")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Anon")
        assert char.origin_label == ""
        assert char.calling_label == ""


# ---------------------------------------------------------------------------
# Case 4c — combined origin choice (elemental_harmony shape). A SINGLE choice
# whose label is the ORIGIN ("The Ember Isles") carries BOTH race_hint and
# class_hint in its mechanical_effects. The choice label names the origin, NOT
# the class — so it must populate origin_label, and calling_label must come from
# the mechanical class_hint ("Channeler"), never the origin display label.
#
# Regression guard for the live burning_peace playtest bug: calling_label was
# being set from the choice display label ("The Ember Isles"), duplicating
# origin_label and cascading into a hollow seed_drive quest + wrong sheet
# identity line ("The Ember Isles · The Ember Isles").
# ---------------------------------------------------------------------------


class TestCombinedOriginChoiceCallingLabel:
    def _ember_isles_scene(self) -> list[CharCreationScene]:
        # elemental_harmony shape: one Origin choice carries race_hint AND
        # class_hint. There is NO separate class-selecting scene (the Discipline
        # is derived from the Origin's class_hint).
        return [
            make_scene(
                "origins",
                choices=[
                    make_choice(
                        "The Ember Isles",
                        description="Born to the fire-touched isles.",
                        race_hint="Ember Isles",
                        class_hint="Channeler",
                    ),
                ],
            ),
        ]

    def test_calling_label_comes_from_class_hint_not_origin_display_label(self) -> None:
        b = CharacterBuilder(scenes=self._ember_isles_scene(), rules=base_rules())
        b.apply_choice(0)
        char = b.build("Chico")
        # The origin display label belongs to origin_label only.
        assert char.origin_label == "The Ember Isles"
        # calling_label must be the mechanical Discipline, NOT the origin label.
        assert char.calling_label == "Channeler"
        # The two display surfaces must not collapse to the same string.
        assert char.origin_label != char.calling_label
        # Mechanical archetypes underneath are unchanged.
        assert char.race == "Ember Isles"
        assert char.char_class == "Channeler"

    def test_seed_drive_fallback_uses_class_hint_not_origin_string(self) -> None:
        """Wiring: a PC built from the combined origin choice with an empty
        drive must NOT seed a hollow quest titled after the origin. With
        calling_label fixed to 'Channeler', the seed-drive fallback (drive →
        calling_label) references the Discipline, never 'The Ember Isles'."""
        from sidequest.game.quest_seed import seed_quest_spine
        from sidequest.game.session import GameSnapshot

        b = CharacterBuilder(scenes=self._ember_isles_scene(), rules=base_rules())
        b.apply_choice(0)
        char = b.build("Chico")
        assert char.drive == ""  # burning_peace defers drive; fallback engages.

        snap = GameSnapshot()
        seed_quest_spine(snap, char)

        # The hollow-seed bug surfaced "The Ember Isles" as the quest title and
        # active_stakes. The origin string must appear in NEITHER.
        assert snap.active_stakes != "The Ember Isles"
        for entry in snap.quest_log.values():
            assert entry.title != "The Ember Isles"
            assert entry.objective != "The Ember Isles"

    def test_identity_description_uses_correct_indefinite_article(self) -> None:
        """Grammar nit bundled with the calling_label fix: 'An Ember Isles
        Channeler', not 'A Ember Isles Channeler' (vowel-initial origin)."""
        b = CharacterBuilder(scenes=self._ember_isles_scene(), rules=base_rules())
        b.apply_choice(0)
        char = b.build("Chico")
        assert char.core.description == "An Ember Isles Channeler"


# ---------------------------------------------------------------------------
# Case 5 — nickname always empty (no chargen source today).
# ---------------------------------------------------------------------------


class TestNicknameAlwaysEmpty:
    def test_nickname_is_empty_after_build(self) -> None:
        """Nickname has no chargen source today; the field is a placeholder
        for a future story. Verify build() never accidentally populates it."""
        scenes = [
            make_scene(
                "origins",
                choices=[
                    make_choice(
                        "Far Landing Raised Me",
                        description="The dust ports raised me.",
                        background="Far Landing-raised",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=base_rules())
        b.apply_choice(0)
        char = b.build("Zanzibar Vesh")
        assert char.nickname == ""
