"""Tests for player_picker field validation in ``_validate_portrait_manifest``
and the ``_collect_poi_slugs`` helper (Epic 66, story 66-Task-6).

Covers:
- picker missing required fields → warning mentions "player_picker" and field names
- picker with dangling backdrop_poi → warning contains the bad slug
- complete picker with valid backdrop_poi → no errors
- npc_major entry with no picker fields → no errors
"""

from __future__ import annotations

from pathlib import Path

import yaml

from sidequest.cli.validate.pack import (
    _collect_poi_slugs,
    _validate_portrait_manifest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a portrait_manifest.yaml under tmp_path and return the path."""
    path = tmp_path / "portrait_manifest.yaml"
    path.write_text(yaml.dump({"characters": entries}), encoding="utf-8")
    return path


def _write_history(tmp_path: Path, poi_slugs: list[str]) -> Path:
    """Write a minimal history.yaml with the given POI slugs under tmp_path."""
    chapters = [
        {
            "id": "ch1",
            "label": "Chapter 1",
            "points_of_interest": [
                {"slug": slug, "name": slug.replace("_", " ").title()}
                for slug in poi_slugs
            ],
        }
    ]
    path = tmp_path / "history.yaml"
    path.write_text(yaml.dump({"chapters": chapters}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPickerFieldValidation:
    def test_picker_missing_required_fields_warns(self, tmp_path: Path) -> None:
        """A player_picker entry missing id/culture/archetype/sex produces a
        warning that mentions 'player_picker' and all missing field names."""
        manifest_path = _write_manifest(
            tmp_path,
            [
                {
                    "name": "Incomplete Picker",
                    "type": "player_picker",
                    # id, culture, archetype, sex all absent
                },
            ],
        )

        _errors, warnings = _validate_portrait_manifest(manifest_path, "pack 'test_pack'")

        assert len(warnings) == 1, f"Expected 1 warning, got: {warnings}"
        msg = warnings[0]
        assert "player_picker" in msg, f"Expected 'player_picker' in message: {msg}"
        for field in ("id", "culture", "archetype", "sex"):
            assert field in msg, f"Expected missing field '{field}' in message: {msg}"

    def test_picker_dangling_backdrop_poi_warns(self, tmp_path: Path) -> None:
        """A player_picker entry whose backdrop_poi is not in known_poi_slugs
        produces a warning containing the dangling slug."""
        manifest_path = _write_manifest(
            tmp_path,
            [
                {
                    "name": "Picker With Bad Backdrop",
                    "type": "player_picker",
                    "id": "picker_a",
                    "culture": "voidborn",
                    "archetype": "drifter",
                    "sex": "female",
                    "backdrop_poi": "nonexistent_poi",
                },
            ],
        )

        _errors, warnings = _validate_portrait_manifest(
            manifest_path,
            "pack 'test_pack'",
            known_poi_slugs={"customs_concourse"},
        )

        assert len(warnings) == 1, f"Expected 1 warning, got: {warnings}"
        assert "nonexistent_poi" in warnings[0], (
            f"Expected dangling slug in message: {warnings[0]}"
        )

    def test_complete_picker_with_valid_backdrop_no_errors(self, tmp_path: Path) -> None:
        """A fully-specified player_picker with a valid backdrop_poi is clean."""
        manifest_path = _write_manifest(
            tmp_path,
            [
                {
                    "name": "Valid Picker",
                    "type": "player_picker",
                    "id": "picker_b",
                    "culture": "voidborn",
                    "archetype": "scout",
                    "sex": "male",
                    "backdrop_poi": "customs_concourse",
                },
            ],
        )

        errors, warnings = _validate_portrait_manifest(
            manifest_path,
            "pack 'test_pack'",
            known_poi_slugs={"customs_concourse"},
        )

        assert errors == [], f"Expected no errors, got: {errors}"
        assert warnings == [], f"Expected no warnings, got: {warnings}"

    def test_npc_major_entry_unaffected(self, tmp_path: Path) -> None:
        """An npc_major entry with no picker fields produces no errors or warnings."""
        manifest_path = _write_manifest(
            tmp_path,
            [
                {
                    "name": "Governor Harwick",
                    "type": "npc_major",
                    "appearance": "Tall man in a worn uniform.",
                    # no id/culture/archetype/sex/backdrop_poi
                },
            ],
        )

        errors, warnings = _validate_portrait_manifest(
            manifest_path,
            "pack 'test_pack'",
            known_poi_slugs={"customs_concourse"},
        )

        assert errors == [], f"NPC entry should not trigger errors, got: {errors}"
        assert warnings == [], f"NPC entry should not trigger picker warnings, got: {warnings}"

    def test_backdrop_poi_not_checked_without_known_slugs(self, tmp_path: Path) -> None:
        """A picker with backdrop_poi set, validated WITHOUT known_poi_slugs
        (None), produces no backdrop warning — the cross-ref check only fires
        when the caller supplies a slug set (the ``is not None`` conditional)."""
        manifest_path = _write_manifest(
            tmp_path,
            [
                {
                    "name": "Picker Without Slug Context",
                    "type": "player_picker",
                    "id": "picker_c",
                    "culture": "voidborn",
                    "archetype": "drifter",
                    "sex": "female",
                    "backdrop_poi": "anything_at_all",
                },
            ],
        )

        errors, warnings = _validate_portrait_manifest(manifest_path, "pack 'test_pack'")

        assert errors == [], f"Expected no errors, got: {errors}"
        assert warnings == [], (
            f"backdrop_poi must not be checked when known_poi_slugs is None, got: {warnings}"
        )


class TestPickerValidationWiring:
    """Wiring test (project rule: every test suite needs one) — exercises the
    production composition exactly as the call site in ``_validate_world`` does:
    ``_collect_poi_slugs(history.yaml)`` feeding ``_validate_portrait_manifest``,
    with both files living in the same world directory."""

    def test_history_slugs_feed_manifest_validation(self, tmp_path: Path) -> None:
        """End-to-end through the production composition: a dangling backdrop_poi
        warns; a valid one (resolved from the same history.yaml) does not."""
        history_path = _write_history(tmp_path, ["vaskov_centrum", "mendes_post"])
        manifest_path = _write_manifest(
            tmp_path,
            [
                {
                    "name": "Picker Good Backdrop",
                    "type": "player_picker",
                    "id": "picker_good",
                    "culture": "voidborn",
                    "archetype": "scout",
                    "sex": "male",
                    "backdrop_poi": "vaskov_centrum",
                },
                {
                    "name": "Picker Dangling Backdrop",
                    "type": "player_picker",
                    "id": "picker_bad",
                    "culture": "voidborn",
                    "archetype": "drifter",
                    "sex": "female",
                    "backdrop_poi": "no_such_poi",
                },
            ],
        )

        # Mirror the call site in _validate_world (pack.py): collect POI slugs
        # from the world's history.yaml, then pass them to the manifest validator.
        poi_slugs = _collect_poi_slugs(history_path)
        errors, warnings = _validate_portrait_manifest(
            manifest_path,
            "world 'test_world'",
            known_poi_slugs=poi_slugs,
        )

        assert errors == [], f"Expected no errors, got: {errors}"
        assert len(warnings) == 1, f"Expected exactly 1 warning, got: {warnings}"
        assert "no_such_poi" in warnings[0], (
            f"Expected dangling slug in warning: {warnings[0]}"
        )
        assert "vaskov_centrum" not in warnings[0], (
            f"Valid backdrop must not warn: {warnings[0]}"
        )


class TestCollectPoiSlugs:
    def test_collects_slugs_from_history(self, tmp_path: Path) -> None:
        """_collect_poi_slugs returns all POI slugs from a history.yaml."""
        _write_history(tmp_path, ["vaskov_centrum", "mendes_post", "deep_root"])
        history_path = tmp_path / "history.yaml"

        slugs = _collect_poi_slugs(history_path)

        assert slugs == {"vaskov_centrum", "mendes_post", "deep_root"}

    def test_absent_history_returns_empty(self, tmp_path: Path) -> None:
        """_collect_poi_slugs returns an empty set when history.yaml is absent."""
        slugs = _collect_poi_slugs(tmp_path / "nonexistent.yaml")
        assert slugs == set()

    def test_multiple_chapters_merged(self, tmp_path: Path) -> None:
        """POI slugs from multiple chapters are all collected."""
        chapters = [
            {
                "id": "ch1",
                "label": "Ch 1",
                "points_of_interest": [{"slug": "poi_a", "name": "POI A"}],
            },
            {
                "id": "ch2",
                "label": "Ch 2",
                "points_of_interest": [{"slug": "poi_b", "name": "POI B"}],
            },
        ]
        path = tmp_path / "history.yaml"
        path.write_text(yaml.dump({"chapters": chapters}), encoding="utf-8")

        slugs = _collect_poi_slugs(path)

        assert slugs == {"poi_a", "poi_b"}
