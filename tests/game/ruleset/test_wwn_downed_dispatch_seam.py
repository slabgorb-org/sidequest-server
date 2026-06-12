"""Unit test: _physical_save_target_for accepts WwnConfig (dispatch seam wiring).

Verifies that after wiring the downed seam to accept "wwn" as well as "cwn",
_physical_save_target_for does NOT raise DiceDispatchError for a WwnConfig.
Also confirms it STILL raises for a plain SwnConfig (the non-cwn/wwn fallback
guard stays live). Full pack-driven end-to-end is deferred to Plan 3 (when the
WWN genre pack lands in content).
"""

from __future__ import annotations

import pytest

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
    SwnConfig,
    WwnConfig,
)
from sidequest.server.dispatch.dice import DiceDispatchError, _physical_save_target_for

_EH_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}

# Minimal WwnConfig with all required fields (save_base=15, attribute_map present).
_WWN_CFG = WwnConfig(attribute_map=_EH_AMAP)

# WwnRulesetModule — the module that would be returned by get_ruleset_module("wwn").
_MOD = WwnRulesetModule()


def _minimal_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="test_wwn",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )


def _minimal_cdef(with_opponent_stats: bool = True) -> ConfrontationDef:
    """ConfrontationDef that carries opponent ability scores for the OPPONENT path."""
    return ConfrontationDef(
        type="combat",
        label="WWN Combat",
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
        opponent_default_stats=(
            {
                # Flavor-name keys as authored in content (attribute_map maps
                # canonical SWN attrs → these flavor names; save_params looks
                # them up by flavor name in the stats dict).
                "Strength": 14,
                "Endurance": 12,
                "Agility": 10,
                "Insight": 8,
                "Spirit": 9,
                "Harmony": 8,
                # Reserved combat-seed keys (popped by opponent_ability_scores()).
                "hp": 8,
                "armor_class": 12,
                "dexterity": 10,
            }
            if with_opponent_stats
            else None
        ),
    )


def _minimal_core() -> CreatureCore:
    return CreatureCore(name="Bandit", description="ruffian", personality="aggressive")


# ---------------------------------------------------------------------------
# Happy-path: WwnConfig is accepted, returns an integer save target.
# ---------------------------------------------------------------------------


def test_physical_save_target_accepts_wwn_config():
    """_physical_save_target_for must NOT raise DiceDispatchError for WwnConfig."""
    snap = _minimal_snapshot()
    cdef = _minimal_cdef(with_opponent_stats=True)
    core = _minimal_core()

    target = _physical_save_target_for(
        ruleset=_MOD,
        snapshot=snap,
        cdef=cdef,
        name="Bandit",
        core=core,
        cfg=_WWN_CFG,
    )
    # save_base=15, level=1 → target = 15 - (1-1) = 15
    assert isinstance(target, int)
    assert target == 15


# ---------------------------------------------------------------------------
# Guard still fires: SwnConfig (not cwn/wwn) must still raise.
# ---------------------------------------------------------------------------


def test_physical_save_target_rejects_swn_config():
    """_physical_save_target_for must still raise DiceDispatchError for raw SwnConfig."""
    snap = _minimal_snapshot()
    cdef = _minimal_cdef(with_opponent_stats=True)
    core = _minimal_core()
    swn_cfg = SwnConfig(attribute_map=_EH_AMAP)

    with pytest.raises(DiceDispatchError, match="CWN/WWN downed seam"):
        _physical_save_target_for(
            ruleset=_MOD,
            snapshot=snap,
            cdef=cdef,
            name="Bandit",
            core=core,
            cfg=swn_cfg,
        )
