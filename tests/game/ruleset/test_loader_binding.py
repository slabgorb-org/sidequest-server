import pytest

from sidequest.game.ruleset import UnknownRulesetError, get_ruleset_module
from sidequest.genre.models.rules import RulesConfig

_EH_FLAVOR = ["Strength", "Agility", "Endurance", "Insight", "Spirit", "Harmony"]
_EH_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}


def test_ruleset_defaults_to_dial():
    rules = RulesConfig()
    assert rules.ruleset == "dial"
    assert get_ruleset_module(rules.ruleset).slug == "dial"


def test_explicit_ruleset_parses():
    rules = RulesConfig(ruleset="dial")
    assert rules.ruleset == "dial"


def test_unknown_ruleset_rejected_at_bind():
    rules = RulesConfig(ruleset="nonsense")
    with pytest.raises(UnknownRulesetError):
        get_ruleset_module(rules.ruleset)


def test_wwn_ruleset_binds():
    from sidequest.game.ruleset.registry import get_ruleset_module
    from sidequest.game.ruleset.wwn import WwnRulesetModule
    from sidequest.genre.models.rules import RulesConfig, WwnConfig

    rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=_EH_FLAVOR,
        wwn=WwnConfig(attribute_map=_EH_AMAP),
    )
    assert isinstance(get_ruleset_module(rules.ruleset), WwnRulesetModule)
