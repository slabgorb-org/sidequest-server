"""Unit tests for confrontation_intent_validator.

Tasks 1+2: tokenize() only. Task 3 adds ValidationResult + validate().
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sidequest.agents.confrontation_intent_validator import (
    ValidationResult,
    tokenize,
    validate,
)

# ---------------------------------------------------------------------------
# tokenize() tests
# ---------------------------------------------------------------------------


def test_tokenize_lowercases() -> None:
    assert tokenize("Haggle") == frozenset({"haggle"})


def test_tokenize_splits_on_non_alphanumeric() -> None:
    assert tokenize("draw-down, fire!") == frozenset({"draw", "down", "fire"})


def test_tokenize_strips_stopwords() -> None:
    result = tokenize("the man with a gun")
    assert "the" not in result
    assert "a" not in result
    assert "with" not in result
    assert "man" in result
    assert "gun" in result


def test_tokenize_suffix_strips_ing_ed_s() -> None:
    assert tokenize("haggling") == frozenset({"haggl"})
    assert tokenize("haggled") == frozenset({"haggl"})
    assert tokenize("offers") == frozenset({"offer"})


def test_tokenize_does_not_porter_stem() -> None:
    # 'draw' and 'drawer' must NOT collapse (Porter would conflate them)
    result = tokenize("draw drawer")
    assert "draw" in result
    assert "drawer" in result


def test_tokenize_empty_input_returns_empty_frozenset() -> None:
    assert tokenize("") == frozenset()
    assert tokenize("   ") == frozenset()


def test_tokenize_idempotent() -> None:
    once = tokenize("Bargaining hard for the horse")
    twice = tokenize(" ".join(sorted(once)))
    assert twice == once


def test_tokenize_does_not_mangle_double_s_words() -> None:
    # 'cross', 'press', 'pass', 'boss' are common confrontation-label
    # words; their tails are 'ss' not pluralization. Don't strip.
    result = tokenize("cross press pass boss class")
    assert "cross" in result
    assert "press" in result
    assert "pass" in result
    assert "boss" in result
    assert "class" in result


# ---------------------------------------------------------------------------
# Test doubles for validate()
# ---------------------------------------------------------------------------


@dataclass
class _FakeActionRewrite:
    you: str = ""
    named: str = ""
    intent: str = ""


@dataclass
class _FakeConfrontationDef:
    confrontation_type: str
    on_intent_mismatch: str
    intent_verb_set: frozenset[str]


@dataclass
class _FakeRules:
    confrontations: list[_FakeConfrontationDef]


@dataclass
class _FakePack:
    rules: _FakeRules


def _pack(*defs: _FakeConfrontationDef) -> _FakePack:
    return _FakePack(rules=_FakeRules(confrontations=list(defs)))


# ---------------------------------------------------------------------------
# validate() tests
# ---------------------------------------------------------------------------


def test_validate_returns_none_when_action_rewrite_is_none() -> None:
    pack = _pack(
        _FakeConfrontationDef("negotiation", "warn", frozenset({"haggle", "bargain"}))
    )
    assert validate(None, "negotiation", pack, active_encounter=False) is None


@pytest.mark.parametrize("intent", ["", "   "])
def test_validate_returns_none_when_intent_is_empty(intent: str) -> None:
    pack = _pack(_FakeConfrontationDef("negotiation", "warn", frozenset({"haggle"})))
    assert (
        validate(_FakeActionRewrite(intent=intent), None, pack, active_encounter=False)
        is None
    )


def test_validate_returns_none_when_pack_is_none() -> None:
    assert (
        validate(
            _FakeActionRewrite(intent="haggle for horse"),
            None,
            None,
            active_encounter=False,
        )
        is None
    )


def test_validate_returns_none_when_active_encounter() -> None:
    pack = _pack(_FakeConfrontationDef("negotiation", "warn", frozenset({"haggle"})))
    result = validate(
        _FakeActionRewrite(intent="haggle for the horse"),
        None,
        pack,
        active_encounter=True,
    )
    assert result is None


def test_validate_returns_none_when_declared_matches_inferred() -> None:
    pack = _pack(
        _FakeConfrontationDef("negotiation", "warn", frozenset({"haggle", "bargain"}))
    )
    result = validate(
        _FakeActionRewrite(intent="haggle for horse price"),
        "negotiation",
        pack,
        active_encounter=False,
    )
    assert result is None


def test_validate_returns_none_when_no_type_matches() -> None:
    pack = _pack(
        _FakeConfrontationDef("negotiation", "warn", frozenset({"haggle"})),
        _FakeConfrontationDef("combat", "reprompt", frozenset({"strike", "fight"})),
    )
    result = validate(
        _FakeActionRewrite(intent="look around the room"),
        None,
        pack,
        active_encounter=False,
    )
    assert result is None


def test_validate_flags_single_mismatch() -> None:
    pack = _pack(
        _FakeConfrontationDef("negotiation", "warn", frozenset({"haggle", "bargain"}))
    )
    result = validate(
        _FakeActionRewrite(intent="bargain hard for the horse"),
        None,
        pack,
        active_encounter=False,
    )
    assert result is not None
    assert result.matched_type == "negotiation"
    assert result.declared is None
    assert result.severity == "warn"
    assert "bargain" in result.matched_tokens


@pytest.mark.parametrize("severity", ["warn", "soft_suggest", "reprompt"])
def test_validate_returns_severity_from_def(severity: str) -> None:
    pack = _pack(_FakeConfrontationDef("x", severity, frozenset({"trigger"})))
    result = validate(
        _FakeActionRewrite(intent="trigger"),
        None,
        pack,
        active_encounter=False,
    )
    assert result is not None
    assert result.severity == severity


def test_validate_multi_match_picks_most_token_overlap() -> None:
    pack = _pack(
        _FakeConfrontationDef("negotiation", "warn", frozenset({"haggle"})),
        _FakeConfrontationDef("poker", "warn", frozenset({"haggle", "bluff", "raise"})),
    )
    # 'haggle' alone matches negotiation; 'haggle bluff' matches both — poker wins.
    result = validate(
        _FakeActionRewrite(intent="haggle and bluff"),
        None,
        pack,
        active_encounter=False,
    )
    assert result is not None
    assert result.matched_type == "poker"


def test_validate_multi_match_tie_broken_by_pack_order() -> None:
    pack = _pack(
        _FakeConfrontationDef("standoff", "reprompt", frozenset({"draw"})),
        _FakeConfrontationDef("duel", "reprompt", frozenset({"draw"})),
    )
    result = validate(
        _FakeActionRewrite(intent="draw"),
        None,
        pack,
        active_encounter=False,
    )
    assert result is not None
    assert result.matched_type == "standoff"  # first declared wins


def test_validate_unknown_declared_type_treated_as_none() -> None:
    pack = _pack(_FakeConfrontationDef("negotiation", "warn", frozenset({"haggle"})))
    result = validate(
        _FakeActionRewrite(intent="haggle for the horse"),
        "nonexistent_type",
        pack,
        active_encounter=False,
    )
    assert result is not None
    assert result.matched_type == "negotiation"


def test_validate_never_raises_on_empty_verb_set() -> None:
    """A def with no derived verbs simply never matches — no raise."""
    pack = _pack(_FakeConfrontationDef("negotiation", "warn", frozenset()))
    assert (
        validate(
            _FakeActionRewrite(intent="anything"), None, pack, active_encounter=False
        )
        is None
    )


def test_validation_result_is_frozen() -> None:
    pack = _pack(_FakeConfrontationDef("negotiation", "warn", frozenset({"haggle"})))
    result = validate(
        _FakeActionRewrite(intent="haggle"),
        None,
        pack,
        active_encounter=False,
    )
    assert result is not None
    assert isinstance(result, ValidationResult)
    with pytest.raises(AttributeError):
        result.matched_type = "other"  # type: ignore[misc]
