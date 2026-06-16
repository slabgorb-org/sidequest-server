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
