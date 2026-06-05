"""AWN downed seam — `_physical_save_target_for` accepts AwnConfig (Story 88-1, Item 8 FREE site).

The Mortal/Major Injury save-target helper at ``dice.py:71`` already uses the
CAPABILITY form ``isinstance(cfg, (CwnConfig, WwnConfig))`` (verified by
test_wwn_downed_dispatch_seam.py for WWN). Because ``AwnConfig`` subclasses
``CwnConfig``, AWN rides this seam FREE — no edit required. This test PROVES
that free coverage: ``_physical_save_target_for`` must NOT raise for an
``AwnConfig`` and must STILL raise for a plain ``SwnConfig`` (the guard stays live).

Mirrors test_wwn_downed_dispatch_seam.py. The run-time span path
(``run_cwn_wwn_downed_seam`` — Item 5, the slug-string ``ruleset in
("cwn","wwn")`` fix) is exercised end-to-end in
tests/server/test_awn_combat_dispatch.py where ``otel_capture`` is available.

Fails until Item 1/3 land (no ``AwnRulesetModule``, no ``AwnConfig``).
"""

from __future__ import annotations

import pytest

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.awn import AwnRulesetModule
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import (
    AwnConfig,
    BeatDef,
    ConfrontationDef,
    MetricDef,
    SwnConfig,
)
from sidequest.server.dispatch.dice import DiceDispatchError, _physical_save_target_for

_AWN_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Dexterity",
    "CONSTITUTION": "Constitution",
    "INTELLIGENCE": "Intelligence",
    "WISDOM": "Wisdom",
    "CHARISMA": "Charisma",
}

_AWN_CFG = AwnConfig(attribute_map=_AWN_AMAP)
_MOD = AwnRulesetModule()


def _minimal_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="test_awn",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )


def _minimal_cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="combat",
        label="AWN Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "strike",
                    "label": "Strike",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STRENGTH",
                }
            ),
        ],
        opponent_default_stats={
            "Strength": 14,
            "Constitution": 12,
            "Dexterity": 10,
            "Intelligence": 8,
            "Wisdom": 9,
            "Charisma": 8,
            # Reserved combat-seed keys (popped by opponent_ability_scores()).
            "hp": 8,
            "armor_class": 12,
            "dexterity": 10,
        },
    )


def _minimal_core() -> CreatureCore:
    return CreatureCore(name="Raider", description="wastelander", personality="feral")


def test_physical_save_target_accepts_awn_config():
    """_physical_save_target_for must NOT raise DiceDispatchError for AwnConfig."""
    target = _physical_save_target_for(
        ruleset=_MOD,
        snapshot=_minimal_snapshot(),
        cdef=_minimal_cdef(),
        name="Raider",
        core=_minimal_core(),
        cfg=_AWN_CFG,
    )
    # save_base=15, level=1 → target = 15 - (1-1) = 15
    assert isinstance(target, int)
    assert target == 15


def test_physical_save_target_still_rejects_swn_config():
    """The non-cwn/wwn guard stays live: a raw SwnConfig must still raise."""
    with pytest.raises(DiceDispatchError, match="CWN/WWN downed seam"):
        _physical_save_target_for(
            ruleset=_MOD,
            snapshot=_minimal_snapshot(),
            cdef=_minimal_cdef(),
            name="Raider",
            core=_minimal_core(),
            cfg=SwnConfig(attribute_map=_AWN_AMAP),
        )
