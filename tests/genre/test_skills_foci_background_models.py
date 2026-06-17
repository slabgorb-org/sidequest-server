"""Tests for Background, Focus, and FocusLevel genre models (ADR-143 Task 7).

These are pure data-model tests — no loader, no accumulation logic.
"""

import pytest
from pydantic import ValidationError

from sidequest.genre.models.character import Background, Focus, FocusLevel


def test_background_grants_skills():
    bg = Background.model_validate(
        {
            "id": "locksmith",
            "display_name": "Locksmith",
            "free_skill": "Sneak",
            "quick_skills": ["Convince", "Work"],
        }
    )
    assert bg.free_skill == "Sneak" and "Work" in bg.quick_skills


def test_focus_levels_grant():
    f = Focus.model_validate(
        {
            "id": "die_hard",
            "display_name": "Die Hard",
            "levels": [{"skills": {"Endure": 1}, "abilities": []}],
        }
    )
    assert f.levels[0].skills["Endure"] == 1


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            Background,
            {"id": "locksmith", "display_name": "Locksmith", "bogus_key": 1},
        ),
        (FocusLevel, {"skills": {"Endure": 1}, "bogus_key": 1}),
        (
            Focus,
            {"id": "die_hard", "display_name": "Die Hard", "bogus_key": 1},
        ),
    ],
)
def test_unknown_field_rejected(model, payload):
    """extra="forbid" guards against typo'd YAML keys (the whole point)."""
    with pytest.raises(ValidationError):
        model.model_validate(payload)
