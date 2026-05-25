"""Tests for ``pf validate pack`` — pack structure presence-gate validator.

Tests are split into two classes:
- ``TestPackSchemaLoading`` — unit tests for schema loading
- ``TestPresenceGate`` — functional tests using the real pack_schema.yaml

The real schema lives at ``sidequest-content/pack_schema.yaml``, resolved
relative to this file (four levels up, then into sidequest-content).

Per project rule: server tests do NOT iterate over live content packs.
All content fixtures are synthesised in ``tmp_path`` per test.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from sidequest.cli.validate.pack import load_pack_schema, validate_pack_structure

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Four levels up from this file: tests/cli/validate/ → tests/cli/ → tests/ →
# sidequest-server/ → oq-2/ ... then into sidequest-content
schema_path_real = (
    Path(__file__).resolve().parents[4] / "sidequest-content" / "pack_schema.yaml"
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _minimal_pack(root: Path) -> Path:
    """Create a minimal genre-pack directory at ``root`` that satisfies ALL
    required files and dirs from the real pack_schema.yaml.

    Required files (18):
        pack.yaml, theme.yaml, archetypes.yaml, tropes.yaml, lore.yaml,
        visual_style.yaml, audio.yaml, rules.yaml, cultures.yaml,
        char_creation.yaml, inventory.yaml, lethality_policy.yaml,
        power_tiers.yaml, progression.yaml, prompts.yaml, axes.yaml,
        visibility_baseline.yaml, client_theme.css

    Required dirs (5):
        audio/music, assets/fonts, assets/images/portraits,
        assets/images/poi, worlds
    """
    required_files = [
        "pack.yaml",
        "theme.yaml",
        "archetypes.yaml",
        "tropes.yaml",
        "lore.yaml",
        "visual_style.yaml",
        "audio.yaml",
        "rules.yaml",
        "cultures.yaml",
        "char_creation.yaml",
        "inventory.yaml",
        "lethality_policy.yaml",
        "power_tiers.yaml",
        "progression.yaml",
        "prompts.yaml",
        "axes.yaml",
        "visibility_baseline.yaml",
        "client_theme.css",
    ]
    required_dirs = [
        "audio/music",
        "assets/fonts",
        "assets/images/portraits",
        "assets/images/poi",
        "worlds",
    ]
    for fname in required_files:
        fpath = root / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.touch()
    for dname in required_dirs:
        (root / dname).mkdir(parents=True, exist_ok=True)
    return root


def _minimal_world(world_dir: Path) -> Path:
    """Create a minimal world directory satisfying ALL required world files/dirs.

    Required files (9):
        world.yaml, cartography.yaml, history.yaml, lore.yaml,
        openings.yaml, portrait_manifest.yaml, tropes.yaml,
        visual_style.yaml, archetypes.yaml

    Required dirs (4):
        cultures, legends, assets/images/portraits, assets/images/poi
    """
    required_files = [
        "world.yaml",
        "cartography.yaml",
        "history.yaml",
        "lore.yaml",
        "openings.yaml",
        "portrait_manifest.yaml",
        "tropes.yaml",
        "visual_style.yaml",
        "archetypes.yaml",
    ]
    required_dirs = [
        "cultures",
        "legends",
        "assets/images/portraits",
        "assets/images/poi",
    ]
    for fname in required_files:
        fpath = world_dir / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.touch()
    for dname in required_dirs:
        (world_dir / dname).mkdir(parents=True, exist_ok=True)
    return world_dir


# ---------------------------------------------------------------------------
# Tests: schema loading
# ---------------------------------------------------------------------------


class TestPackSchemaLoading:
    def test_load_schema(self, tmp_path: Path) -> None:
        """load_pack_schema parses a minimal schema YAML and returns the dict."""
        schema = {
            "schema_version": "1.0",
            "genre_pack": {
                "required_files": ["pack.yaml"],
                "required_dirs": ["worlds"],
                "extensions": {
                    "magic": {"files": ["magic.yaml"]},
                },
            },
            "world": {
                "required_files": ["world.yaml"],
                "required_dirs": ["cultures"],
                "extensions": {},
            },
        }
        schema_file = tmp_path / "test_schema.yaml"
        schema_file.write_text(yaml.dump(schema), encoding="utf-8")

        result = load_pack_schema(schema_file)

        assert "genre_pack" in result
        assert "world" in result
        assert result["genre_pack"]["required_files"] == ["pack.yaml"]
        assert result["genre_pack"]["required_dirs"] == ["worlds"]
        assert "magic" in result["genre_pack"]["extensions"]


# ---------------------------------------------------------------------------
# Tests: presence gate
# ---------------------------------------------------------------------------


class TestPresenceGate:
    def test_valid_pack_passes(self, tmp_path: Path) -> None:
        """A complete pack with one complete world produces zero errors."""
        pack_dir = tmp_path / "my_pack"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)

        world_dir = pack_dir / "worlds" / "test_world"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)

        errors, warnings = validate_pack_structure(pack_dir, schema_path_real)

        assert errors == [], f"Unexpected errors: {errors}"

    def test_missing_required_file_is_error(self, tmp_path: Path) -> None:
        """Removing theme.yaml from a complete pack produces an error mentioning it."""
        pack_dir = tmp_path / "my_pack"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)

        # Add a world so world-level validation doesn't add noise
        world_dir = pack_dir / "worlds" / "test_world"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)

        # Remove required file
        (pack_dir / "theme.yaml").unlink()

        errors, _warnings = validate_pack_structure(pack_dir, schema_path_real)

        assert any("theme.yaml" in e for e in errors), (
            f"Expected error mentioning 'theme.yaml', got: {errors}"
        )

    def test_declared_extension_missing_is_error(self, tmp_path: Path) -> None:
        """pack.yaml that declares magic extension but has no magic.yaml → error."""
        pack_dir = tmp_path / "my_pack"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)

        # Override pack.yaml to declare the magic extension
        (pack_dir / "pack.yaml").write_text(
            "extensions: [magic]\n", encoding="utf-8"
        )

        world_dir = pack_dir / "worlds" / "test_world"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)

        errors, _warnings = validate_pack_structure(pack_dir, schema_path_real)

        assert any("magic.yaml" in e for e in errors), (
            f"Expected error mentioning 'magic.yaml', got: {errors}"
        )

    def test_orphan_file_is_warning(self, tmp_path: Path) -> None:
        """A file not in the schema produces a warning, not an error."""
        pack_dir = tmp_path / "my_pack"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)

        # Add mystery file
        (pack_dir / "mystery_file.yaml").write_text("key: value\n", encoding="utf-8")

        world_dir = pack_dir / "worlds" / "test_world"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)

        errors, warnings = validate_pack_structure(pack_dir, schema_path_real)

        assert errors == [], f"Unexpected errors: {errors}"
        assert any("mystery_file.yaml" in w for w in warnings), (
            f"Expected warning mentioning 'mystery_file.yaml', got: {warnings}"
        )

    def test_draft_world_warnings_not_errors(self, tmp_path: Path) -> None:
        """A world marked draft: true produces warnings instead of errors for missing files."""
        pack_dir = tmp_path / "my_pack"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)

        # Create a draft world that is missing required files
        world_dir = pack_dir / "worlds" / "draft_world"
        world_dir.mkdir(parents=True)
        # Only add world.yaml with draft: true — skip everything else
        (world_dir / "world.yaml").write_text("draft: true\n", encoding="utf-8")

        errors, warnings = validate_pack_structure(pack_dir, schema_path_real)

        # No errors — everything should be demoted to warnings
        assert errors == [], f"Expected no errors for draft world, got: {errors}"
        # Should have warnings about missing files
        assert len(warnings) > 0, "Expected warnings for missing world files in draft world"
