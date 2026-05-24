"""Unit tests for reference_visibility module."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sidequest.server.reference_visibility import (
    KEEPER,
    PUBLIC,
    Visibility,
    _match_pattern,
    _validate_pattern,
    classify,
)


def test_classify_public_via_stem_default() -> None:
    # 'lore' is in PUBLIC_STEMS; 'history' has no KEEPER pattern → PUBLIC.
    assert classify("lore", ("history",)) is Visibility.PUBLIC


def test_classify_keeper_exact_match() -> None:
    assert classify("tropes", ()) is Visibility.KEEPER


def test_classify_unknown_for_unknown_stem() -> None:
    # 'archetype_constraints' is NOT in PUBLIC_STEMS — never rendered.
    assert classify("archetype_constraints", ("genre_flavor",)) is Visibility.UNKNOWN


def test_classify_wildcard_matches_list_item_field() -> None:
    # When the dispatcher substitutes '*' for a list-of-dict slot, the
    # query path looks like ("factions", "*", "name") which the stem-
    # default PUBLIC still resolves to PUBLIC.
    assert classify("lore", ("factions", "*", "name")) is Visibility.PUBLIC


def test_keeper_subtree_match() -> None:
    # ('beat_vocabulary', ('obstacles',)) is a KEEPER subtree marker.
    assert classify("beat_vocabulary", ("obstacles",)) is Visibility.KEEPER
    # Children covered by enumerated wildcard entries.
    assert classify("beat_vocabulary", ("obstacles", "*", "name")) is Visibility.KEEPER


def test_keeper_narrator_hint_under_rules() -> None:
    assert (
        classify("rules", ("confrontations", "*", "beats", "*", "narrator_hint"))
        is Visibility.KEEPER
    )
    assert (
        classify("rules", ("edge_config", "thresholds", "*", "narrator_hint")) is Visibility.KEEPER
    )
    assert (
        classify("rules", ("resources", "*", "thresholds", "*", "narrator_hint"))
        is Visibility.KEEPER
    )


def test_keeper_power_tiers_npc_via_dict_key_wildcard() -> None:
    # Class name is the pattern's first '*' segment; tier slot the second.
    assert classify("power_tiers", ("Fighter", "*", "npc")) is Visibility.KEEPER
    assert classify("power_tiers", ("Detective", "*", "npc")) is Visibility.KEEPER


def test_public_and_keeper_are_disjoint() -> None:
    overlap = PUBLIC & KEEPER
    assert not overlap, f"Overlap between PUBLIC and KEEPER: {overlap}"


def test_wildcard_depth_three_or_greater_rejected_at_import() -> None:
    with pytest.raises(ValueError, match="depth"):
        _validate_pattern(("a", "*", "b", "*", "c", "*"))


def test_match_pattern_length_mismatch_fails() -> None:
    assert _match_pattern(("a", "*"), ("a", "b", "c")) is False


def test_match_pattern_wildcard_matches_any_segment() -> None:
    assert _match_pattern(("a", "*", "c"), ("a", "literal_key", "c")) is True
    assert _match_pattern(("a", "*", "c"), ("a", "*", "c")) is True


def test_validator_reports_unknown_stem_outside_public_stems(tmp_path: Path) -> None:
    """The validator reports fields under unknown stems — only renderer-
    reachable stems are PUBLIC by default."""
    from sidequest.cli.validate_reference_visibility import scan_pack

    pack_dir = tmp_path / "fake_pack"
    pack_dir.mkdir()
    (pack_dir / "unknown_stem.yaml").write_text(yaml.safe_dump({"some_field": "leak risk"}))

    unclassified = scan_pack(pack_dir)
    assert ("unknown_stem", ("some_field",)) in unclassified


def test_validator_clean_on_public_stem(tmp_path: Path) -> None:
    """A file under a PUBLIC_STEMS stem with arbitrary content classifies
    cleanly — stem-default PUBLIC covers unknown fields."""
    from sidequest.cli.validate_reference_visibility import scan_pack

    pack_dir = tmp_path / "fake_pack"
    pack_dir.mkdir()
    (pack_dir / "lore.yaml").write_text(
        yaml.safe_dump(
            {
                "history": "Real prose.",
                "totally_arbitrary_field": "still classifies",
                "factions": [
                    {"name": "X", "summary": "y"},
                    {"name": "Q", "bogus_extension": "fine"},
                ],
            }
        )
    )
    assert scan_pack(pack_dir) == []
