import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import ConfrontationDef, GeometryModifiers


def _minimal_confrontation(**overrides) -> ConfrontationDef:
    """Build a minimal valid ConfrontationDef (social/dial defaults).

    Required fields: type/label/category plus at least one beat
    (id/label/kind/stat_check) and player/opponent metrics (the default
    dial_threshold win_condition requires both). category=social keeps it
    off the hp_depletion reserved-key validator.
    """
    kwargs = {
        "type": "test_clash",
        "label": "Test Clash",
        "category": "social",
        "player_metric": {"name": "advantage", "threshold": 3},
        "opponent_metric": {"name": "pressure", "threshold": 3},
        "beats": [
            {
                "id": "jab",
                "label": "Jab",
                "kind": "push",
                "stat_check": "wit",
            }
        ],
    }
    kwargs.update(overrides)
    return ConfrontationDef(**kwargs)


def test_geometry_modifiers_defaults_empty():
    gm = GeometryModifiers()
    assert gm.aspect == {}
    assert gm.range == {}


def test_geometry_modifiers_loads_authored_calibration():
    gm = GeometryModifiers(
        aspect={"tail_on": 2, "quartering": 1, "crossing": -1, "head_on": -2},
        range={"gun": 2, "close": 0, "medium": -2, "far": -4},
    )
    assert gm.aspect["tail_on"] == 2
    assert gm.range["far"] == -4


def test_geometry_modifiers_rejects_unknown_key():
    with pytest.raises(ValidationError):
        GeometryModifiers(aspect={}, range={}, bogus=1)


def test_confrontation_player_stat_accessors():
    conf = _minimal_confrontation(player_default_stats={"hp": 6, "armor_class": 14})
    assert conf.player_hp == 6
    assert conf.player_armor_class == 14


def test_confrontation_player_stat_accessors_empty():
    conf = _minimal_confrontation(player_default_stats={})
    assert conf.player_hp is None
    assert conf.player_armor_class is None
