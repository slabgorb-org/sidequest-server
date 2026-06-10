"""Story 93-2 — durable ``creation_answers`` provenance on the Character.

Today the player's chargen answers are orphaned: freeform text is consumed
for the ``{class}`` prose slot then discarded, and selections survive only
as flavor labels. 93-2 adds an ordered ``creation_answers`` provenance list
to the Character — one entry per ANSWERED chargen scene, each carrying
``scene_id``, ``prompt`` (the scene's title), ``kind`` ('choice' |
'freeform'), ``value`` (the player's verbatim freeform text OR the chosen
option label), and an ``archetype_inferred`` marker — populated in
``builder.build()`` from the SceneResults already captured.

Contract pinned by these tests (the Dev implements TO this surface):

- ``sidequest.game.character.CreationAnswer`` — pydantic model,
  ``extra="forbid"``, ``kind`` is a closed Literal, ``archetype_inferred``
  defaults False.
- ``Character.creation_answers: list[CreationAnswer]`` — defaults to []
  so pre-93-2 saves still validate.
- ``CharacterBuilder.build()`` populates the list in scene-walk order.
- Scenes the player never ANSWERED (auto-advance display scenes, the
  arrangement confirm) contribute NO entry — the History section lists
  prompt/answer pairs, and an un-answered scene has no answer to show.
- Revert-safe: a popped SceneResult drops its answer (same doctrine as
  ``freeform_answer_texts``).

All scenes here are synthetic — content invariants belong to the pack
validator, never unit tests. The real-content wiring test lives at
``tests/integration/test_creation_answers_wiring.py``.
"""

from __future__ import annotations

import pydantic
import pytest

from sidequest.game.builder import CharacterBuilder, StoryInput
from sidequest.game.character import Character, CreationAnswer
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

# ---------------------------------------------------------------------------
# Synthetic-scene helpers (pattern: tests/game/test_builder_freeform_class_label.py)
# ---------------------------------------------------------------------------


def make_choice(label: str, **effect_fields: object) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description="A description.",
        mechanical_effects=MechanicalEffects(**effect_fields),  # type: ignore[arg-type]
    )


def make_scene(
    scene_id: str,
    *,
    title: str = "Scene title",
    choices: list[CharCreationChoice] | None = None,
    allows_freeform: bool | None = None,
    mechanical_effects: MechanicalEffects | None = None,
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title=title,
        narration="Scene narration.",
        choices=choices or [],
        allows_freeform=allows_freeform,
        mechanical_effects=mechanical_effects,
    )


def simple_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class="Detective",
        default_race="Human",
    )


_VERBATIM_FREEFORM = (
    "I was born in the slag-quarters — third child of a debt-bonded smith; "
    "I protect what is mine,   whitespace and all."
)


def _origin_scene() -> CharCreationScene:
    return make_scene(
        "the_origin",
        title="Where do you come from?",
        choices=[
            make_choice("The Village Itself", race_hint="Human"),
            make_choice("The Drift", race_hint="Synthetic"),
        ],
        allows_freeform=True,
    )


def _calling_scene() -> CharCreationScene:
    return make_scene(
        "the_calling",
        title="What is your calling?",
        choices=[
            make_choice("Country Doctor", class_hint="Doctor"),
            make_choice("Village Constable", class_hint="Detective"),
        ],
        allows_freeform=True,
    )


def _drive_scene() -> CharCreationScene:
    """A drive-shaped scene: backstory effects, no race/class hints (AC3:
    drive scenes must be represented faithfully, not dropped)."""
    return make_scene(
        "the_drive",
        title="What drives you?",
        choices=[
            make_choice("Revenge against the Combine", goals="Seek vengeance"),
            make_choice("A debt unpaid", goals="Repay a debt"),
        ],
    )


def _display_scene() -> CharCreationScene:
    """Auto-advance display scene — no choices, no freeform, never answered."""
    return make_scene("the_mouth", title="The Mouth", allows_freeform=False)


def _name_scene() -> CharCreationScene:
    """Terminal name-entry scene: no choices + allows_freeform."""
    return make_scene("the_name", title="What is your name?", allows_freeform=True)


# ---------------------------------------------------------------------------
# CreationAnswer model contract
# ---------------------------------------------------------------------------


class TestCreationAnswerModel:
    def test_fields_and_defaults(self) -> None:
        entry = CreationAnswer(
            scene_id="the_origin",
            prompt="Where do you come from?",
            kind="choice",
            value="The Village Itself",
        )
        assert entry.scene_id == "the_origin"
        assert entry.prompt == "Where do you come from?"
        assert entry.kind == "choice"
        assert entry.value == "The Village Itself"
        assert entry.archetype_inferred is False, (
            "archetype_inferred must default False — only the 93-1 inference "
            "seam marks it True"
        )

    def test_kind_is_closed_literal(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            CreationAnswer(
                scene_id="s",
                prompt="p",
                kind="interpretive_dance",  # type: ignore[arg-type]
                value="v",
            )

    def test_unknown_keys_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            CreationAnswer(
                scene_id="s",
                prompt="p",
                kind="freeform",
                value="v",
                surprise="nope",  # type: ignore[call-arg]
            )

    def test_character_creation_answers_defaults_empty(self) -> None:
        """Pre-93-2 saves carry no creation_answers — the field must default
        to [] so old characters still validate on load."""
        character = _minimal_character()
        assert character.creation_answers == []


def _minimal_character(**overrides: object) -> Character:
    kwargs: dict = dict(
        core=CreatureCore(
            name="Thorn",
            description="A test subject",
            personality="Determined",
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        backstory="A wanderer with a test past",
        char_class="Detective",
        race="Human",
    )
    kwargs.update(overrides)
    return Character(**kwargs)


# ---------------------------------------------------------------------------
# builder.build() population — choice flow
# ---------------------------------------------------------------------------


class TestChoiceFlowAnswers:
    def test_preset_build_records_one_entry_per_answered_scene(self) -> None:
        """AC3: each preset entry has kind='choice' and value = the chosen
        OPTION LABEL (not the description, not the mechanical hint)."""
        b = CharacterBuilder(
            scenes=[_origin_scene(), _calling_scene(), _drive_scene()],
            rules=simple_rules(),
        )
        b.apply_choice(0)  # The Village Itself
        b.apply_choice(1)  # Village Constable
        b.apply_choice(0)  # Revenge against the Combine
        character = b.build("Thorn")

        answers = character.creation_answers
        assert len(answers) == 3, (
            f"three answered scenes must yield three entries, got "
            f"{[(a.scene_id, a.value) for a in answers]}"
        )
        assert [a.scene_id for a in answers] == ["the_origin", "the_calling", "the_drive"]
        assert all(a.kind == "choice" for a in answers)
        assert [a.value for a in answers] == [
            "The Village Itself",
            "Village Constable",
            "Revenge against the Combine",
        ]

    def test_prompt_is_the_scene_title(self) -> None:
        b = CharacterBuilder(scenes=[_origin_scene()], rules=simple_rules())
        b.apply_choice(0)
        character = b.build("Thorn")
        assert character.creation_answers[0].prompt == "Where do you come from?"

    def test_drive_scene_represented_faithfully(self) -> None:
        """AC3 names drive scenes explicitly — a scene with only backstory
        effects (no race/class hints) must not be dropped from provenance."""
        b = CharacterBuilder(scenes=[_drive_scene()], rules=simple_rules())
        b.apply_choice(1)
        character = b.build("Thorn")
        assert [(a.scene_id, a.kind, a.value) for a in character.creation_answers] == [
            ("the_drive", "choice", "A debt unpaid"),
        ]

    def test_combined_origin_scene_represented_faithfully(self) -> None:
        """AC3: a combined-origin choice (race_hint + class_hint on one
        option, elemental_harmony style) is still ONE answer carrying the
        chosen label."""
        combined = make_scene(
            "the_homeland",
            title="Which homeland claims you?",
            choices=[
                make_choice("The Ember Isles", race_hint="Islander", class_hint="Elementalist"),
                make_choice("The Salt Reach", race_hint="Reacher", class_hint="Tidecaller"),
            ],
        )
        b = CharacterBuilder(scenes=[combined], rules=simple_rules())
        b.apply_choice(0)
        character = b.build("Thorn")
        assert [(a.scene_id, a.kind, a.value) for a in character.creation_answers] == [
            ("the_homeland", "choice", "The Ember Isles"),
        ]

    def test_build_never_marks_archetype_inferred(self) -> None:
        """build() itself must not mark inference — that happens at the
        93-1 confirm seam AFTER a successful Haiku inference, never on the
        preset path."""
        b = CharacterBuilder(
            scenes=[_origin_scene(), _calling_scene()],
            rules=simple_rules(),
        )
        b.apply_choice(0)
        b.apply_choice(0)
        character = b.build("Thorn")
        assert character.creation_answers, "preset build must record answers"
        assert all(a.archetype_inferred is False for a in character.creation_answers)


# ---------------------------------------------------------------------------
# builder.build() population — freeform flow
# ---------------------------------------------------------------------------


class TestFreeformFlowAnswers:
    def test_freeform_value_is_verbatim_not_derived(self) -> None:
        """AC2: the stored value is the player's VERBATIM text — punctuation,
        em-dash, internal whitespace — not a derived label. (The derived
        label channel, ``freeform_class_label``, strips articles and
        flavor; provenance must not.)"""
        b = CharacterBuilder(
            scenes=[_origin_scene(), _calling_scene()],
            rules=simple_rules(),
        )
        b.apply_freeform(_VERBATIM_FREEFORM)
        b.apply_freeform("A vegetarian and temperance lecturer — forever ignored.")
        character = b.build("Thorn")

        answers = character.creation_answers
        assert len(answers) == 2
        assert all(a.kind == "freeform" for a in answers)
        assert answers[0].value == _VERBATIM_FREEFORM, (
            "freeform provenance must be the player's exact words; got "
            f"{answers[0].value!r}"
        )
        assert answers[1].value == ("A vegetarian and temperance lecturer — forever ignored.")
        assert [a.scene_id for a in answers] == ["the_origin", "the_calling"]

    def test_mixed_flow_preserves_scene_walk_order(self) -> None:
        b = CharacterBuilder(
            scenes=[_origin_scene(), _calling_scene(), _drive_scene()],
            rules=simple_rules(),
        )
        b.apply_choice(0)
        b.apply_freeform(_VERBATIM_FREEFORM)
        b.apply_choice(1)
        character = b.build("Thorn")

        assert [(a.scene_id, a.kind) for a in character.creation_answers] == [
            ("the_origin", "choice"),
            ("the_calling", "freeform"),
            ("the_drive", "choice"),
        ]

    def test_name_scene_answer_is_recorded(self) -> None:
        """The terminal name-entry scene IS an answered scene — one entry
        per answered scene means the name answer appears too (the 93-3
        History section decides presentation, not the provenance layer)."""
        b = CharacterBuilder(
            scenes=[_origin_scene(), _name_scene()],
            rules=simple_rules(),
        )
        b.apply_choice(0)
        b.apply_freeform("Dejah Voss")
        character = b.build("Dejah Voss")

        assert [(a.scene_id, a.kind, a.value) for a in character.creation_answers] == [
            ("the_origin", "choice", "The Village Itself"),
            ("the_name", "freeform", "Dejah Voss"),
        ]

    def test_story_input_recorded_as_freeform_with_player_words(self) -> None:
        """the_story (StoryInput) is an answered scene: kind='freeform' and
        the value carries the player's verbatim background and description
        text (the pronouns ride MechanicalEffects, not the answer value)."""
        story = make_scene("the_story", title="Tell us your story")
        b = CharacterBuilder(
            scenes=[_origin_scene(), story],
            rules=simple_rules(),
        )
        b.apply_choice(0)
        b.apply_response(
            StoryInput(
                pronouns="they/them",
                background="Raised in the caverns.",
                description="Steadfast, candlelit, scarred.",
            )
        )
        character = b.build("Thorn")

        assert len(character.creation_answers) == 2
        story_answer = character.creation_answers[1]
        assert story_answer.scene_id == "the_story"
        assert story_answer.kind == "freeform"
        assert "Raised in the caverns." in story_answer.value
        assert "Steadfast, candlelit, scarred." in story_answer.value


# ---------------------------------------------------------------------------
# Un-answered scenes and revert safety
# ---------------------------------------------------------------------------


class TestUnansweredAndRevert:
    def test_auto_advance_display_scene_contributes_no_entry(self) -> None:
        """A display-only scene the player merely acked is not an ANSWERED
        scene — no prompt/answer pair exists to show in History."""
        b = CharacterBuilder(
            scenes=[_origin_scene(), _display_scene(), _calling_scene()],
            rules=simple_rules(),
        )
        b.apply_choice(0)
        b.apply_auto_advance()
        b.apply_choice(0)
        character = b.build("Thorn")

        assert [a.scene_id for a in character.creation_answers] == [
            "the_origin",
            "the_calling",
        ], "the auto-advance display scene must not appear in creation_answers"

    def test_go_back_drops_the_reverted_answer(self) -> None:
        """Revert-safe (same doctrine as freeform_answer_texts): going back
        pops the SceneResult, so the final build reflects only the answer
        the player actually settled on — no duplicates, no ghosts."""
        b = CharacterBuilder(
            scenes=[_origin_scene(), _calling_scene()],
            rules=simple_rules(),
        )
        b.apply_choice(0)
        b.apply_freeform("First answer, abandoned.")
        b.go_back()
        b.apply_choice(1)  # settle on Village Constable instead
        character = b.build("Thorn")

        answers = character.creation_answers
        assert len(answers) == 2, f"got {[(a.scene_id, a.value) for a in answers]}"
        assert answers[1].scene_id == "the_calling"
        assert answers[1].kind == "choice"
        assert answers[1].value == "Village Constable"
        assert all("abandoned" not in a.value for a in answers), (
            "the reverted freeform answer must not survive in provenance"
        )


# ---------------------------------------------------------------------------
# Persistence: round-trips, persisted-not-recomputed
# ---------------------------------------------------------------------------


class TestCreationAnswersPersistence:
    def test_character_json_round_trip_preserves_answers(self) -> None:
        character = _minimal_character(
            creation_answers=[
                CreationAnswer(
                    scene_id="the_origin",
                    prompt="Where do you come from?",
                    kind="freeform",
                    value=_VERBATIM_FREEFORM,
                    archetype_inferred=True,
                ),
                CreationAnswer(
                    scene_id="the_name",
                    prompt="What is your name?",
                    kind="freeform",
                    value="Dejah Voss",
                ),
            ]
        )
        restored = Character.model_validate_json(character.model_dump_json())
        assert restored.creation_answers == character.creation_answers

    def test_snapshot_round_trip_preserves_inferred_marker(self) -> None:
        """AC4: persisted, not recomputed. The archetype_inferred=True flag
        only exists because the inference fired at confirm time — a reload
        that recomputed provenance would lose it. Round-tripping through
        the GameSnapshot JSON (the persistence substrate the PG snapshot
        store writes) must preserve it exactly."""
        character = _minimal_character(
            creation_answers=[
                CreationAnswer(
                    scene_id="the_origin",
                    prompt="Where do you come from?",
                    kind="freeform",
                    value=_VERBATIM_FREEFORM,
                    archetype_inferred=True,
                ),
            ]
        )
        snapshot = GameSnapshot(
            genre_slug="test_genre",
            world_slug="test_world",
            characters=[character],
        )
        restored = GameSnapshot.model_validate_json(snapshot.model_dump_json())
        assert restored.characters[0].creation_answers == character.creation_answers
        assert restored.characters[0].creation_answers[0].archetype_inferred is True
