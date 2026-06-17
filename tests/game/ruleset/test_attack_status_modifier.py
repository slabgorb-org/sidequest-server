"""Attack rolls read the aggregate status roll_modifier (Phase 2, light & darkness).

Covers the SWN chokepoint (SWN/CWN/AWN/WWN all inherit ``SwnRulesetModule.attack_params``
unchanged) and the Native dial module. The contract is ``dark == lit - 2``: a status
carrying ``roll_modifier=-2`` (e.g. fighting in the dark) shifts the attack modifier by
exactly that amount and nothing else changes.
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.dial import DialRulesetModule
from sidequest.game.ruleset.swn import SwnRulesetModule
from sidequest.game.status import Status, StatusSeverity
from sidequest.genre.models.rules import BeatDef


def _core(name: str) -> CreatureCore:
    return CreatureCore(name=name, description="a delver", personality="grim")


def _dark_core() -> CreatureCore:
    core = _core("Delver")
    core.statuses.append(
        Status(text="in the dark", severity=StatusSeverity.Wound, roll_modifier=-2)
    )
    return core


def _beat() -> BeatDef:
    return BeatDef(
        id="strike",
        label="Strike",
        kind="strike",
        stat_check="STRENGTH",
        combat_skill=0,
        attack_bonus=0,
    )


def test_swn_attack_applies_darkness_penalty():
    ruleset = SwnRulesetModule()
    beat = _beat()
    stats = {
        "STRENGTH": 10,
        "DEXTERITY": 10,
        "CONSTITUTION": 10,
        "INTELLIGENCE": 10,
        "WISDOM": 10,
        "CHARISMA": 10,
    }
    lit = ruleset.attack_params(
        beat=beat, attacker_stats=stats, attacker_core=_core("L"), target_core=None
    )
    dark = ruleset.attack_params(
        beat=beat, attacker_stats=stats, attacker_core=_dark_core(), target_core=None
    )
    assert dark.modifier == lit.modifier - 2
    # target_number is untouched by the status modifier.
    assert dark.target_number == lit.target_number


def test_dial_attack_applies_darkness_penalty():
    ruleset = DialRulesetModule()
    beat = _beat()
    stats = {"STRENGTH": 10}
    lit = ruleset.attack_params(
        beat=beat, attacker_stats=stats, attacker_core=_core("L"), target_core=None
    )
    dark = ruleset.attack_params(
        beat=beat, attacker_stats=stats, attacker_core=_dark_core(), target_core=None
    )
    assert dark.modifier == lit.modifier - 2
    assert dark.target_number == lit.target_number
