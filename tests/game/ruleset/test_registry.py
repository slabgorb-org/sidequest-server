import pytest

from sidequest.game.ruleset import UnknownRulesetError, get_ruleset_module
from sidequest.game.ruleset.native import NativeRulesetModule


def test_native_resolves():
    assert isinstance(get_ruleset_module("native"), NativeRulesetModule)


def test_native_is_singleton():
    assert get_ruleset_module("native") is get_ruleset_module("native")


def test_unknown_ruleset_fails_loud():
    with pytest.raises(UnknownRulesetError) as exc:
        get_ruleset_module("no_such_ruleset")
    assert "no_such_ruleset" in str(exc.value)


def test_cwn_registered():
    from sidequest.game.ruleset.cwn import CwnRulesetModule
    from sidequest.game.ruleset.registry import get_ruleset_module

    module = get_ruleset_module("cwn")
    assert isinstance(module, CwnRulesetModule)
    assert module.slug == "cwn"


def test_wwn_registered_and_singleton():
    from sidequest.game.ruleset.registry import get_ruleset_module
    from sidequest.game.ruleset.wwn import WwnRulesetModule

    mod = get_ruleset_module("wwn")
    assert isinstance(mod, WwnRulesetModule)
    assert get_ruleset_module("wwn") is mod  # stateless singleton
