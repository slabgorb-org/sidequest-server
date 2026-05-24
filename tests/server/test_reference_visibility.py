"""Unit tests for reference_visibility module."""

from __future__ import annotations

import pytest

from sidequest.server.reference_visibility import (
    KEEPER,
    PUBLIC,
    Visibility,
    classify,
)


def test_classify_public_exact_match() -> None:
    assert classify("lore", ("history",)) is Visibility.PUBLIC


def test_classify_keeper_exact_match() -> None:
    assert classify("tropes", ()) is Visibility.KEEPER


def test_classify_unknown_returns_unknown() -> None:
    assert classify("lore", ("totally_made_up_field",)) is Visibility.UNKNOWN


def test_classify_wildcard_matches_list_item_field() -> None:
    # ("lore", ("factions", "*", "name")) is in PUBLIC
    # Any concrete index must resolve as PUBLIC.
    assert classify("lore", ("factions", "*", "name")) is Visibility.PUBLIC


def test_public_and_keeper_are_disjoint() -> None:
    # No (stem, path) tuple may live in both sets.
    overlap = PUBLIC & KEEPER
    assert not overlap, f"Overlap between PUBLIC and KEEPER: {overlap}"


def test_wildcard_depth_three_or_greater_rejected_at_import() -> None:
    # Build a fake set with depth-3 wildcard; classify_pattern should reject.
    from sidequest.server.reference_visibility import _validate_pattern

    with pytest.raises(ValueError, match="depth"):
        _validate_pattern(("a", "*", "b", "*", "c", "*"))
