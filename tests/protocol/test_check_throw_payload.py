"""Tests for CheckThrowPayload cross-field validation.

These are TDD RED tests — they fail before the @model_validator is added
to CheckThrowPayload.  They assert ValidationError for the three
malformed-input cases that would otherwise produce an opaque crash deep
in dispatch_check / SwnRulesetModule.

Fix 1: skill_check missing attribute or difficulty_key → ValidationError
Fix 2: save missing save category → ValidationError
       bogus kind → ValidationError
Positive: valid skill_check and valid save construct without error.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol.messages import CheckThrowPayload

# ---------------------------------------------------------------------------
# Fix 1 — skill_check cross-field validation
# ---------------------------------------------------------------------------


def test_skill_check_missing_attribute_raises():
    """skill_check without attribute must raise ValidationError, not pass silently."""
    with pytest.raises(ValidationError, match="skill_check requires"):
        CheckThrowPayload(
            kind="skill_check",
            attribute=None,  # missing
            difficulty_key="tricky",
            faces=[4, 5],
        )


def test_skill_check_missing_difficulty_key_raises():
    """skill_check without difficulty_key must raise ValidationError."""
    with pytest.raises(ValidationError, match="skill_check requires"):
        CheckThrowPayload(
            kind="skill_check",
            attribute="DEXTERITY",
            difficulty_key=None,  # missing
            faces=[4, 5],
        )


# ---------------------------------------------------------------------------
# Fix 2 — save + bogus kind
# ---------------------------------------------------------------------------


def test_save_missing_save_category_raises():
    """save without save category must raise ValidationError, not pass silently."""
    with pytest.raises(ValidationError, match="save requires"):
        CheckThrowPayload(
            kind="save",
            save=None,  # missing
            faces=[13],
        )


def test_bogus_kind_raises():
    """Unrecognised kind must raise ValidationError."""
    with pytest.raises(ValidationError, match="must be 'skill_check' or 'save'"):
        CheckThrowPayload(
            kind="bogus",
            faces=[10],
        )


# ---------------------------------------------------------------------------
# Positive cases — valid payloads must not raise
# ---------------------------------------------------------------------------


def test_valid_skill_check_constructs_ok():
    payload = CheckThrowPayload(
        kind="skill_check",
        attribute="DEXTERITY",
        difficulty_key="tricky",
        skill_level=2,
        label="Notice trap",
        faces=[4, 5],
    )
    assert payload.kind == "skill_check"
    assert payload.attribute == "DEXTERITY"
    assert payload.difficulty_key == "tricky"


def test_valid_save_constructs_ok():
    payload = CheckThrowPayload(
        kind="save",
        save="mental",
        label="Mental save",
        faces=[13],
    )
    assert payload.kind == "save"
    assert payload.save == "mental"
