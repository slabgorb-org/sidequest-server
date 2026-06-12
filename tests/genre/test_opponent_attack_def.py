"""ConfrontationDef.opponent_attack — the content-authored enemy attack profile
that drives the SWN beat_selection hp_depletion enemy turn (playtest perseus_cloud).

Mirrors the opponent_hp / opponent_armor_class reserved-key pattern: the opponent's
reprisal is explicit content (No Silent Fallbacks), not a hidden default mook.
"""

from __future__ import annotations

import pytest

from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import ConfrontationDef, OpponentAttackDef


def test_opponent_attack_def_carries_stat_attack_bonus_and_damage():
    oa = OpponentAttackDef.model_validate(
        {
            "stat_check": "Physique",
            "attack_bonus": 1,
            "combat_skill": 1,
            "damage": {"dice": "1d6", "bonus": 0},
        }
    )
    assert oa.stat_check == "Physique"
    assert oa.attack_bonus == 1
    assert oa.combat_skill == 1
    assert isinstance(oa.damage, DamageSpec)
    assert oa.damage.dice == "1d6"


def test_opponent_attack_requires_damage():
    """No silent fallback: an enemy attack with no damage spec is a content bug."""
    with pytest.raises((ValueError, TypeError)):
        OpponentAttackDef.model_validate({"stat_check": "Physique"})


def test_confrontation_def_accepts_opponent_attack():
    cdef = ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Firefight",
            "category": "combat",
            "resolution_mode": "beat_selection",
            "win_condition": "hp_depletion",
            "opponent_default_stats": {"Physique": 10, "hp": 7, "armor_class": 12, "dexterity": 13},
            "opponent_attack": {
                "stat_check": "Physique",
                "attack_bonus": 1,
                "combat_skill": 1,
                "damage": {"dice": "1d6", "bonus": 0},
            },
            "beats": [
                {
                    "id": "shoot",
                    "label": "Shoot",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "Physique",
                    "damage_channel": "strike",
                }
            ],
        }
    )
    assert cdef.opponent_attack is not None
    assert cdef.opponent_attack.stat_check == "Physique"
    assert cdef.opponent_attack.damage.dice == "1d6"


def test_confrontation_def_opponent_attack_defaults_none():
    """Absent opponent_attack is allowed (confrontations with no enemy reprisal,
    e.g. dial/opposed_check social) — the engine step no-ops when it's None."""
    cdef = ConfrontationDef.model_validate(
        {
            "type": "negotiation",
            "label": "Parley",
            "category": "social",
            "player_metric": {"name": "leverage", "starting": 0, "threshold": 7},
            "opponent_metric": {"name": "leverage", "starting": 0, "threshold": 7},
            "beats": [
                {
                    "id": "persuade",
                    "label": "Persuade",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "Cunning",
                }
            ],
        }
    )
    assert cdef.opponent_attack is None
