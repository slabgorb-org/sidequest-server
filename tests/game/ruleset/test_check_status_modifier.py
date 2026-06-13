"""Task 2.3: skill checks and saves read the status roll_modifier.

The marquee path — a failed find-the-rope-back search/navigation roll in the
dark kills by degrees because the in-the-dark Status (roll_modifier=-2) drags
the d20/2d6 down. These tests pin that the status term reaches both the SWN
check_params (2d6 skill) and save_params (d20) modifiers, and that omitting
character_core stays back-compat (no kwarg required, no status term applied).
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.swn import SwnRulesetModule
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.game.status import Status, StatusSeverity


def _clean_core() -> CreatureCore:
    return CreatureCore(name="Delver", description="a delver", personality="cautious")


def _dark_core() -> CreatureCore:
    core = CreatureCore(name="Delver", description="a delver", personality="cautious")
    core.statuses.append(
        Status(text="in the dark", severity=StatusSeverity.Wound, roll_modifier=-2)
    )
    return core


def test_skill_check_applies_darkness_penalty() -> None:
    rs = SwnRulesetModule()
    stats = {"wis": 12}
    base = rs.check_params(
        stats=stats,
        attribute="wis",
        skill_level=1,
        difficulty_key="normal",
        label="search",
        cfg=_Cfg(),
        character_core=_clean_core(),
    )
    dark = rs.check_params(
        stats=stats,
        attribute="wis",
        skill_level=1,
        difficulty_key="normal",
        label="search",
        cfg=_Cfg(),
        character_core=_dark_core(),
    )
    assert dark.modifier == base.modifier - 2


def test_check_params_core_defaults_none() -> None:
    # back-compat: omitting character_core must not raise and yields no status mod
    rs = SwnRulesetModule()
    p = rs.check_params(
        stats={"wis": 12},
        attribute="wis",
        skill_level=1,
        difficulty_key="normal",
        label="search",
        cfg=_Cfg(),
    )
    assert isinstance(p.modifier, int)


def test_save_applies_darkness_penalty() -> None:
    rs = SwnRulesetModule()
    stats = {"con": 12, "str": 12}
    base = rs.save_params(
        stats=stats, save="physical", level=1, label="endure", cfg=_Cfg(), character_core=_clean_core()
    )
    dark = rs.save_params(
        stats=stats, save="physical", level=1, label="endure", cfg=_Cfg(), character_core=_dark_core()
    )
    assert dark.modifier == base.modifier - 2


def test_save_params_core_defaults_none() -> None:
    rs = SwnRulesetModule()
    p = rs.save_params(stats={"con": 12, "str": 12}, save="physical", level=1, label="endure", cfg=_Cfg())
    assert isinstance(p.modifier, int)


def test_cwn_luck_save_applies_darkness_penalty() -> None:
    # The CWN luck save computes a player d20 modifier (attributeless, base 0);
    # the status term must still reach it.
    rs = CwnRulesetModule()
    base = rs.save_params(
        stats={}, save="luck", level=1, label="luck", cfg=_Cfg(), character_core=_clean_core()
    )
    dark = rs.save_params(
        stats={}, save="luck", level=1, label="luck", cfg=_Cfg(), character_core=_dark_core()
    )
    assert dark.modifier == base.modifier - 2


def test_wwn_luck_save_applies_darkness_penalty() -> None:
    rs = WwnRulesetModule()
    base = rs.save_params(
        stats={}, save="luck", level=1, label="luck", cfg=_Cfg(), character_core=_clean_core()
    )
    dark = rs.save_params(
        stats={}, save="luck", level=1, label="luck", cfg=_Cfg(), character_core=_dark_core()
    )
    assert dark.modifier == base.modifier - 2


class _Cfg:
    """Minimal RulesConfig stand-in for the two compute paths under test.

    SWN check_params reads cfg.difficulties[key]; save_params reads
    cfg.save_base, cfg.attribute_map. Values are arbitrary but well-formed
    (canonical SWN attribute names mapped to themselves so stat_modifier
    resolves against the provided stats dict)."""

    difficulties = {"normal": 8}
    save_base = 15

    @property
    def attribute_map(self) -> dict[str, str]:
        return {
            "STRENGTH": "str",
            "CONSTITUTION": "con",
            "DEXTERITY": "dex",
            "INTELLIGENCE": "int",
            "WISDOM": "wis",
            "CHARISMA": "cha",
        }
