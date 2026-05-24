"""Unit tests for reference_visibility module."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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


def test_validator_reports_unknown_field(tmp_path: Path) -> None:
    """The validator walks a pack dir and reports every (stem, key_path) not
    in PUBLIC ∪ KEEPER."""
    from sidequest.cli.validate_reference_visibility import scan_pack

    # Build a tiny synthetic pack
    pack_dir = tmp_path / "fake_pack"
    pack_dir.mkdir()
    (pack_dir / "lore.yaml").write_text(
        yaml.safe_dump(
            {
                "history": "Real prose.",  # PUBLIC
                "totally_made_up_field": "leak risk",  # UNKNOWN
            }
        )
    )

    unclassified = scan_pack(pack_dir)
    assert ("lore", ("totally_made_up_field",)) in unclassified
    assert ("lore", ("history",)) not in unclassified


def test_validator_walks_nested_list_of_dicts(tmp_path: Path) -> None:
    from sidequest.cli.validate_reference_visibility import scan_pack

    pack_dir = tmp_path / "fake_pack"
    pack_dir.mkdir()
    (pack_dir / "lore.yaml").write_text(
        yaml.safe_dump(
            {
                "factions": [
                    {"name": "X", "summary": "y", "description": "z", "disposition": "neutral"},
                    {"name": "Q", "bogus": "leak"},
                ]
            }
        )
    )

    unclassified = scan_pack(pack_dir)
    # name/summary/description/disposition are PUBLIC under ("factions", "*", …)
    assert ("lore", ("factions", "*", "bogus")) in unclassified
    assert ("lore", ("factions", "*", "name")) not in unclassified
