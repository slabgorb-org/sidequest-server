"""Unit tests for confrontation_intent_validator.

This task: tokenize() only. validate() lands in Task 3.
"""

from __future__ import annotations

from sidequest.agents.confrontation_intent_validator import tokenize


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
    assert twice <= once
