"""Tests for sidequest.game.builder — Slice 1: pure types.

Ports the type-shape tests from sidequest_game::builder (builder.rs).
This file covers the type layer only — BuilderPhase variants, SceneInputType
variants, SceneResult/AccumulatedChoices defaults, BuilderError subclasses.

Behavior tests (scene walking, stat gen, build finalizer) land in later
slices with their own test files.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from sidequest.game.builder import (
    CONFIRMATION,
    AccumulatedChoices,
    AwaitingFollowup,
    BuilderError,
    CannotRevertError,
    ChoiceInput,
    Confirmation,
    FreeformInput,
    FreeformNotAllowedError,
    HookType,
    HpConfigMissingClassError,
    InProgress,
    InvalidChoiceError,
    LoreAnchor,
    NarrativeHook,
    NoScenesError,
    NumericNameError,
    SceneResult,
    UnknownStatGenerationError,
    WrongPhaseError,
)
from sidequest.genre.models.character import MechanicalEffects

# ---------------------------------------------------------------------------
# HookType — enum values match Rust variant names
# ---------------------------------------------------------------------------


class TestHookType:
    def test_all_variants_present(self) -> None:
        expected = {
            "Origin",
            "Wound",
            "Relationship",
            "Goal",
            "Trait",
            "Debt",
            "Secret",
            "Possession",
        }
        assert {h.value for h in HookType} == expected

    def test_is_string_enum(self) -> None:
        # String enum: the value equals the string.
        assert HookType.ORIGIN == "Origin"
        assert HookType.POSSESSION.value == "Possession"


# ---------------------------------------------------------------------------
# NarrativeHook — default mechanical_key, equality
# ---------------------------------------------------------------------------


class TestNarrativeHook:
    def test_construct_with_all_fields(self) -> None:
        h = NarrativeHook(
            hook_type=HookType.ORIGIN,
            source_scene="origin_scene",
            text="Origin: Mutant",
            mechanical_key="race_hint",
        )
        assert h.hook_type == HookType.ORIGIN
        assert h.source_scene == "origin_scene"
        assert h.text == "Origin: Mutant"
        assert h.mechanical_key == "race_hint"

    def test_mechanical_key_optional(self) -> None:
        h = NarrativeHook(hook_type=HookType.WOUND, source_scene="s", text="t")
        assert h.mechanical_key is None

    def test_equality_by_value(self) -> None:
        a = NarrativeHook(hook_type=HookType.GOAL, source_scene="s", text="t")
        b = NarrativeHook(hook_type=HookType.GOAL, source_scene="s", text="t")
        assert a == b


# ---------------------------------------------------------------------------
# LoreAnchor — 3-field dataclass, all required
# ---------------------------------------------------------------------------


class TestLoreAnchor:
    def test_construct(self) -> None:
        a = LoreAnchor(anchor_type="npc", value="Thessa", source_scene="relationship_scene")
        assert a.anchor_type == "npc"
        assert a.value == "Thessa"
        assert a.source_scene == "relationship_scene"


# ---------------------------------------------------------------------------
# SceneInputType variants — ChoiceInput / FreeformInput
# ---------------------------------------------------------------------------


class TestSceneInputType:
    def test_choice_input_carries_index(self) -> None:
        c = ChoiceInput(index=2)
        assert c.index == 2

    def test_freeform_input_carries_text(self) -> None:
        f = FreeformInput(text="Kara")
        assert f.text == "Kara"

    def test_variants_are_distinct_types(self) -> None:
        c = ChoiceInput(index=0)
        f = FreeformInput(text="foo")
        assert isinstance(c, ChoiceInput)
        assert isinstance(f, FreeformInput)
        assert not isinstance(c, FreeformInput)
        assert not isinstance(f, ChoiceInput)

    def test_frozen(self) -> None:
        # Frozen dataclasses — mutation raises FrozenInstanceError.
        c = ChoiceInput(index=0)
        with pytest.raises(FrozenInstanceError):  # dataclasses.FrozenInstanceError
            c.index = 1  # type: ignore[misc]

    def test_match_statement_dispatches(self) -> None:
        """The tagged-union usage pattern — match on concrete subclass."""
        inputs: list = [ChoiceInput(index=3), FreeformInput(text="Rux")]
        labels: list[str] = []
        for item in inputs:
            match item:
                case ChoiceInput(index=i):
                    labels.append(f"choice:{i}")
                case FreeformInput(text=t):
                    labels.append(f"free:{t}")
        assert labels == ["choice:3", "free:Rux"]


# ---------------------------------------------------------------------------
# SceneResult — default list/dict fields, required fields
# ---------------------------------------------------------------------------


class TestSceneResult:
    def test_construct_minimal(self) -> None:
        eff = MechanicalEffects()
        r = SceneResult(input_type=ChoiceInput(index=0), effects_applied=eff)
        assert isinstance(r.input_type, ChoiceInput)
        assert r.effects_applied is eff
        assert r.hooks_added == []
        assert r.anchors_added == []
        assert r.choice_description is None

    def test_construct_full(self) -> None:
        eff = MechanicalEffects(class_hint="Ranger")
        hooks = [NarrativeHook(hook_type=HookType.TRAIT, source_scene="s", text="Class: Ranger")]
        anchors = [LoreAnchor(anchor_type="npc", value="Thessa", source_scene="s")]
        r = SceneResult(
            input_type=FreeformInput(text="Kara"),
            effects_applied=eff,
            hooks_added=hooks,
            anchors_added=anchors,
            choice_description="A wry survivor",
        )
        assert r.hooks_added == hooks
        assert r.anchors_added == anchors
        assert r.choice_description == "A wry survivor"

    def test_default_lists_are_independent_instances(self) -> None:
        """Guard against the classic mutable-default pitfall — each new
        SceneResult must get its own list, not share a class-level one."""
        r1 = SceneResult(input_type=ChoiceInput(index=0), effects_applied=MechanicalEffects())
        r2 = SceneResult(input_type=ChoiceInput(index=1), effects_applied=MechanicalEffects())
        r1.hooks_added.append(NarrativeHook(hook_type=HookType.GOAL, source_scene="s", text="t"))
        assert r2.hooks_added == []


# ---------------------------------------------------------------------------
# AccumulatedChoices — defaults match Rust Default impl
# ---------------------------------------------------------------------------


class TestAccumulatedChoices:
    def test_default_is_all_empty(self) -> None:
        acc = AccumulatedChoices()
        # Optional[str] fields default to None
        assert acc.class_hint is None
        assert acc.race_hint is None
        assert acc.personality_trait is None
        assert acc.affinity_hint is None
        assert acc.background is None
        assert acc.mutation_hint is None
        assert acc.training_hint is None
        assert acc.emotional_state is None
        assert acc.relationship is None
        assert acc.goals is None
        assert acc.rig_type_hint is None
        assert acc.rig_trait is None
        assert acc.catch_phrase is None
        assert acc.pronoun_hint is None
        assert acc.jungian_hint is None
        assert acc.rpg_role_hint is None
        assert acc.reputation_bonus is None
        # Collection fields default empty
        assert acc.item_hints == []
        assert acc.backstory_fragments == []
        assert acc.stat_bonuses == {}

    def test_independent_collections(self) -> None:
        """Each AccumulatedChoices gets its own list/dict instances."""
        a = AccumulatedChoices()
        b = AccumulatedChoices()
        a.item_hints.append("crowbar")
        a.stat_bonuses["STR"] = 2
        a.backstory_fragments.append("A wanderer.")
        assert b.item_hints == []
        assert b.stat_bonuses == {}
        assert b.backstory_fragments == []

    def test_reputation_bonus_field_exists(self) -> None:
        """Wiring check for the Phase 1 IOU: the field is present on the
        accumulated-view type. Actual accumulation logic lands in Slice 2."""
        acc = AccumulatedChoices(reputation_bonus="intimidation")
        assert acc.reputation_bonus == "intimidation"


# ---------------------------------------------------------------------------
# BuilderPhase variants — InProgress / AwaitingFollowup / Confirmation
# ---------------------------------------------------------------------------


class TestBuilderPhase:
    def test_in_progress_carries_scene_index(self) -> None:
        p = InProgress(scene_index=3)
        assert p.scene_index == 3

    def test_awaiting_followup_carries_index_and_prompt(self) -> None:
        p = AwaitingFollowup(scene_index=2, hook_prompt="Tell me about your scar.")
        assert p.scene_index == 2
        assert p.hook_prompt == "Tell me about your scar."

    def test_confirmation_is_dataless_singleton(self) -> None:
        assert isinstance(CONFIRMATION, Confirmation)
        # CONFIRMATION is a module-level instance — cheap to compare by identity.
        assert CONFIRMATION is CONFIRMATION

    def test_variants_are_distinct_types(self) -> None:
        a = InProgress(scene_index=0)
        b = AwaitingFollowup(scene_index=0, hook_prompt="p")
        c = Confirmation()
        assert isinstance(a, InProgress)
        assert isinstance(b, AwaitingFollowup)
        assert isinstance(c, Confirmation)
        assert not isinstance(a, AwaitingFollowup)
        assert not isinstance(a, Confirmation)

    def test_frozen(self) -> None:
        p = InProgress(scene_index=0)
        with pytest.raises(FrozenInstanceError):
            p.scene_index = 1  # type: ignore[misc]

    def test_match_statement_dispatches(self) -> None:
        """Tagged-union usage pattern — match on concrete subclass with deconstruction."""
        phases: list = [
            InProgress(scene_index=2),
            AwaitingFollowup(scene_index=3, hook_prompt="tell me more"),
            Confirmation(),
        ]
        labels: list[str] = []
        for p in phases:
            match p:
                case InProgress(scene_index=i):
                    labels.append(f"in_progress:{i}")
                case AwaitingFollowup(scene_index=i, hook_prompt=hp):
                    labels.append(f"followup:{i}:{hp}")
                case Confirmation():
                    labels.append("confirmation")
        assert labels == ["in_progress:2", "followup:3:tell me more", "confirmation"]


# ---------------------------------------------------------------------------
# BuilderError hierarchy — all variants catchable via BuilderError base
# ---------------------------------------------------------------------------


class TestBuilderError:
    def test_all_subclasses_inherit_from_base(self) -> None:
        subclasses = [
            InvalidChoiceError(index=3, max_index=2),
            WrongPhaseError(expected="InProgress", actual="Confirmation"),
            FreeformNotAllowedError(),
            NoScenesError(),
            CannotRevertError(),
            UnknownStatGenerationError(method="weird"),
            NumericNameError(name="7"),
            HpConfigMissingClassError(class_name="Ranger"),
        ]
        for err in subclasses:
            assert isinstance(err, BuilderError)

    def test_invalid_choice_carries_indices(self) -> None:
        e = InvalidChoiceError(index=5, max_index=2)
        assert e.index == 5
        assert e.max_index == 2
        assert "invalid choice" in str(e)
        assert "5" in str(e)
        assert "2" in str(e)

    def test_wrong_phase_carries_labels(self) -> None:
        e = WrongPhaseError(expected="InProgress", actual="Confirmation")
        assert e.expected == "InProgress"
        assert e.actual == "Confirmation"
        assert "InProgress" in str(e)
        assert "Confirmation" in str(e)

    def test_unknown_stat_generation_carries_method(self) -> None:
        e = UnknownStatGenerationError(method="roll_5d20")
        assert e.method == "roll_5d20"
        assert "roll_5d20" in str(e)

    def test_numeric_name_carries_name(self) -> None:
        e = NumericNameError(name="7")
        assert e.name == "7"
        assert "'7'" in str(e)
        assert "numeric" in str(e)

    def test_hp_config_missing_class_carries_class(self) -> None:
        e = HpConfigMissingClassError(class_name="Ranger")
        assert e.class_name == "Ranger"
        assert "Ranger" in str(e)

    def test_subclass_aliases_accessible_via_base(self) -> None:
        """Callers can catch specific variants via the base class namespace,
        matching Rust's `BuilderError::InvalidChoice { .. }` read pattern."""
        assert BuilderError.InvalidChoice is InvalidChoiceError
        assert BuilderError.WrongPhase is WrongPhaseError
        assert BuilderError.FreeformNotAllowed is FreeformNotAllowedError
        assert BuilderError.NoScenes is NoScenesError
        assert BuilderError.CannotRevert is CannotRevertError
        assert BuilderError.UnknownStatGeneration is UnknownStatGenerationError
        assert BuilderError.NumericName is NumericNameError
        assert BuilderError.HpConfigMissingClass is HpConfigMissingClassError

    def test_base_catches_subclass(self) -> None:
        """Idiomatic catch: except BuilderError catches any variant."""
        with pytest.raises(BuilderError):
            raise InvalidChoiceError(index=0, max_index=0)

    def test_specific_catch_via_alias(self) -> None:
        """Callers can catch via the aliased name for precision."""
        with pytest.raises(BuilderError.InvalidChoice):
            raise InvalidChoiceError(index=0, max_index=0)
