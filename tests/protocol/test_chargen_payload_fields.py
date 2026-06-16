"""Tests for the_arrangement / the_story chargen scene fields on
CharacterCreationPayload, plus the new ClassRequirement nested model.

Task 4.1 of docs/superpowers/plans/2026-05-09-cnc-chargen-big-improvements.md.

Architectural note: rather than introducing parallel ArrangementPayload /
StoryPayload classes, we extend the existing single CharacterCreationPayload
with optional fields — matching the existing per-scene-shape pattern.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol.messages import CharacterCreationPayload
from sidequest.protocol.models import ClassRequirement


def test_pool_field_accepts_list_of_int():
    p = CharacterCreationPayload(pool=[12, 9, 15, 8, 14, 11])
    assert p.pool == [12, 9, 15, 8, 14, 11]


def test_assignment_field_accepts_dict_with_optional_int():
    p = CharacterCreationPayload(
        assignment={"STR": 14, "DEX": 12, "CON": 10, "INT": None, "WIS": None, "CHA": None}
    )
    assert p.assignment["STR"] == 14
    assert p.assignment["INT"] is None


def test_qualifying_classes_field_accepts_list_of_str():
    p = CharacterCreationPayload(qualifying_classes=["Fighter", "Thief"])
    assert p.qualifying_classes == ["Fighter", "Thief"]


def test_class_requirements_field_accepts_list_of_class_requirement():
    p = CharacterCreationPayload(
        class_requirements=[
            ClassRequirement(name="Fighter", requirement_label="STR 9+"),
            ClassRequirement(name="Mage", requirement_label="INT 9+"),
        ],
    )
    assert p.class_requirements[0].name == "Fighter"
    assert p.class_requirements[1].requirement_label == "INT 9+"


def test_confirm_enabled_field_accepts_bool():
    assert CharacterCreationPayload(confirm_enabled=True).confirm_enabled is True
    assert CharacterCreationPayload(confirm_enabled=False).confirm_enabled is False


def test_pronouns_options_field_accepts_list_of_str():
    p = CharacterCreationPayload(pronouns_options=["she/her", "he/him", "they/them"])
    assert p.pronouns_options == ["she/her", "he/him", "they/them"]


def test_pronouns_allow_freeform_field_accepts_bool():
    assert CharacterCreationPayload(pronouns_allow_freeform=True).pronouns_allow_freeform is True


def test_background_optional_and_description_optional_fields_accept_bool():
    p = CharacterCreationPayload(background_optional=True, description_optional=False)
    assert p.background_optional is True
    assert p.description_optional is False


def test_autogen_available_field_accepts_bool():
    assert CharacterCreationPayload(autogen_available=True).autogen_available is True


def test_autogen_result_field_accepts_dict():
    p = CharacterCreationPayload(
        autogen_result={"background": "Former ratcatcher.", "description": ""}
    )
    assert p.autogen_result["background"] == "Former ratcatcher."


def test_arrange_client_request_fields_accepted():
    """Client→server payload uses same model with `phase` discriminator."""
    p = CharacterCreationPayload(phase="arrange_assign", stat="STR", value=14)
    assert p.phase == "arrange_assign"
    assert p.stat == "STR"
    assert p.value == 14


def test_story_client_request_fields_accepted():
    p = CharacterCreationPayload(
        phase="story_confirm",
        pronouns="they/them",
        background="Former ratcatcher.",
        description="Tall, soot-stained.",
    )
    assert p.pronouns == "they/them"
    assert p.background == "Former ratcatcher."
    assert p.description == "Tall, soot-stained."


def test_class_requirement_extra_forbid():
    with pytest.raises(ValidationError):
        ClassRequirement(name="X", requirement_label="Y", garbage=True)
