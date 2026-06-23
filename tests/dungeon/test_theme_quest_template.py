# tests/dungeon/test_theme_quest_template.py
import pytest
from pydantic import ValidationError

from sidequest.dungeon.themes import DungeonTheme

_BASE = {
    "id": "bone_crypt", "display_name": "Bone Crypt",
    "generator_class": "built",
    "interior": {"algorithm": "roomcorridor", "params": {}},
    "depth_band": {"min": 0.0, "max": 50.0},
    "narrator": {"register": "grim", "flavor": "ossuary", "motifs": ["bone"]},
}

def test_quest_template_parses_big_bad():
    theme = DungeonTheme.model_validate(
        {**_BASE, "quest_template": {
            "signature": "big_bad",
            "title": "The {theme} Stirs",
            "objective": "Something rules the {theme}. Find it and end it.",
        }}
    )
    assert theme.quest_template is not None
    assert theme.quest_template.signature == "big_bad"

def test_quest_template_optional():
    theme = DungeonTheme.model_validate(_BASE)
    assert theme.quest_template is None

def test_quest_template_rejects_unknown_signature():
    with pytest.raises(ValidationError):
        DungeonTheme.model_validate(
            {**_BASE, "quest_template": {"signature": "boss_rush", "title": "x", "objective": "y"}}
        )
