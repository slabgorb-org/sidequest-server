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


# ---------------------------------------------------------------------------
# Story 88-1 — AWN ruleset module registration (Items 1 & 2)
# ---------------------------------------------------------------------------


def test_get_ruleset_module_awn_resolves():
    from sidequest.game.ruleset.awn import AwnRulesetModule
    from sidequest.game.ruleset.registry import get_ruleset_module

    module = get_ruleset_module("awn")
    assert isinstance(module, AwnRulesetModule)
    assert module.slug == "awn"


def test_awn_module_is_singleton():
    from sidequest.game.ruleset.registry import get_ruleset_module

    assert get_ruleset_module("awn") is get_ruleset_module("awn")  # stateless singleton


def test_awn_module_is_a_clean_without_number_sibling():
    # ADR-142: AWN combat == WN-core combat. AWN reparents directly onto
    # WithoutNumberRulesetModule (a clean sibling), NOT onto CwnRulesetModule —
    # the old Awn(Cwn) chain is dismantled. The WN-core capability checks
    # (isinstance(module, WithoutNumberRulesetModule)) cover AWN; AWN is NOT a CWN.
    from sidequest.game.ruleset.awn import AwnRulesetModule
    from sidequest.game.ruleset.cwn import CwnRulesetModule
    from sidequest.game.ruleset.registry import get_ruleset_module
    from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule

    assert issubclass(AwnRulesetModule, WithoutNumberRulesetModule)
    assert not issubclass(AwnRulesetModule, CwnRulesetModule)
    assert isinstance(get_ruleset_module("awn"), WithoutNumberRulesetModule)
    assert not isinstance(get_ruleset_module("awn"), CwnRulesetModule)


def test_unknown_ruleset_still_fails_loud_after_awn():
    # Regression guard for the fail-loud contract: registering "awn" must not
    # introduce a silent default for unknown slugs (No Silent Fallbacks).
    with pytest.raises(UnknownRulesetError) as exc:
        get_ruleset_module("ashes")  # close to "awn" but not registered
    assert "ashes" in str(exc.value)


# ---------------------------------------------------------------------------
# ADR-144 F1a — Fate ruleset module registration
# ---------------------------------------------------------------------------


def test_fate_registered_and_singleton():
    from sidequest.game.ruleset.fate import FateRulesetModule
    from sidequest.game.ruleset.registry import get_ruleset_module

    module = get_ruleset_module("fate")
    assert isinstance(module, FateRulesetModule)
    assert module.slug == "fate"
    assert get_ruleset_module("fate") is module  # stateless singleton


def test_unknown_ruleset_still_fails_loud_after_fate():
    # Registering "fate" must not introduce a silent default (No Silent Fallbacks).
    with pytest.raises(UnknownRulesetError) as exc:
        get_ruleset_module("fudge")  # close to "fate" but not registered
    assert "fudge" in str(exc.value)
