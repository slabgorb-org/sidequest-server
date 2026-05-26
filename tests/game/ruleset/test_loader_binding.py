import pytest

from sidequest.game.ruleset import UnknownRulesetError, get_ruleset_module
from sidequest.genre.models.rules import RulesConfig


def test_ruleset_defaults_to_native():
    rules = RulesConfig()
    assert rules.ruleset == "native"
    assert get_ruleset_module(rules.ruleset).slug == "native"


def test_explicit_ruleset_parses():
    rules = RulesConfig(ruleset="native")
    assert rules.ruleset == "native"


def test_unknown_ruleset_rejected_at_bind():
    rules = RulesConfig(ruleset="nonsense")
    with pytest.raises(UnknownRulesetError):
        get_ruleset_module(rules.ruleset)
