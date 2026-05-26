import pytest

from sidequest.game.ruleset import UnknownRulesetError, get_ruleset_module
from sidequest.game.ruleset.native import NativeRulesetModule


def test_native_resolves():
    assert isinstance(get_ruleset_module("native"), NativeRulesetModule)


def test_native_is_singleton():
    assert get_ruleset_module("native") is get_ruleset_module("native")


def test_unknown_ruleset_fails_loud():
    with pytest.raises(UnknownRulesetError) as exc:
        get_ruleset_module("swn")  # not registered until the SWN plan lands
    assert "swn" in str(exc.value)
