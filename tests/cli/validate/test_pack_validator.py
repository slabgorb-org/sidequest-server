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

import pytest
import yaml

from sidequest.cli.validate.pack import load_pack_schema, validate_pack_structure

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Four levels up from this file: tests/cli/validate/ → tests/cli/ → tests/ →
# sidequest-server/ → oq-2/ ... then into sidequest-content
schema_path_real = Path(__file__).resolve().parents[4] / "sidequest-content" / "pack_schema.yaml"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _minimal_pack(root: Path) -> Path:
    """Create a minimal genre-pack directory at ``root`` that satisfies ALL
    required files and dirs from the real pack_schema.yaml.

    Required files (17):
        pack.yaml, theme.yaml, archetypes.yaml, tropes.yaml,
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
        # lore.yaml is forbidden at the genre tier (epic-74 story 74-3) — do NOT
        # create one here or the validator flags it.
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
    # World lore must seed a non-empty LoreStore (epic-74 story 74-3): an empty
    # touch fails the validator's seedable-lore rule, so write minimal seedable
    # content instead.
    (world_dir / "lore.yaml").write_text(
        "world_name: Test World\nhistory: A minimal but seedable world history.\n",
        encoding="utf-8",
    )
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
        (pack_dir / "pack.yaml").write_text("extensions: [magic]\n", encoding="utf-8")

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

    def test_genre_cultures_yaml_is_optional(self, tmp_path: Path) -> None:
        """A genre pack missing cultures.yaml passes (epic-74: cultures live in worlds).

        Mirrors space_opera, whose genre cultures.yaml was deliberately deleted.
        """
        pack_dir = tmp_path / "my_pack"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        (pack_dir / "cultures.yaml").unlink()  # delete the genre-tier cultures.yaml

        errors, _warnings = validate_pack_structure(pack_dir, schema_path_real)

        assert not any("cultures.yaml" in e for e in errors), (
            f"genre cultures.yaml must be optional; got: {errors}"
        )

    def test_incomplete_waiver_demotes_named_missing_file_to_warning(self, tmp_path: Path) -> None:
        """A NON-draft world may waive a specific loader-optional required artifact.

        ``incomplete_files`` in world.yaml demotes ONLY the named missing file to
        a loud WAIVED warning (live-but-incomplete world, e.g. an imported
        campaign skeleton whose canon must not be fabricated). A different missing
        required file that is NOT waived still hard-errors.
        """
        pack_dir = tmp_path / "my_pack"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)

        world_dir = pack_dir / "worlds" / "live_incomplete"
        _minimal_world(world_dir)
        # Make it live (non-draft) and waive tropes.yaml + the legends dir, then
        # actually remove those artifacts so the waiver has something to absorb.
        (world_dir / "world.yaml").write_text(
            "draft: false\nincomplete_files:\n  - tropes.yaml\nincomplete_dirs:\n  - legends\n",
            encoding="utf-8",
        )
        (world_dir / "tropes.yaml").unlink()
        (world_dir / "legends").rmdir()
        # A non-waived required file is removed too — this MUST still error.
        (world_dir / "portrait_manifest.yaml").unlink()

        errors, warnings = validate_pack_structure(pack_dir, schema_path_real)

        # Waived artifacts: no error, but a loud WAIVED warning each.
        assert not any("tropes.yaml" in e for e in errors), (
            f"waived tropes.yaml should not error; got: {errors}"
        )
        assert not any("'legends'" in e for e in errors), (
            f"waived legends dir should not error; got: {errors}"
        )
        assert any("WAIVED" in w and "tropes.yaml" in w for w in warnings), (
            f"expected a loud WAIVED warning for tropes.yaml; got: {warnings}"
        )
        assert any("WAIVED" in w and "legends" in w for w in warnings), (
            f"expected a loud WAIVED warning for legends dir; got: {warnings}"
        )
        # Non-waived missing required file still hard-errors (waiver is scoped).
        assert any("portrait_manifest.yaml" in e for e in errors), (
            f"non-waived missing portrait_manifest.yaml must still error; got: {errors}"
        )


# ---------------------------------------------------------------------------
# Tests: content validation (Story 64-4)
#
# The presence gate above only checks that files EXIST. This class drives the
# new content-validation pass: each present, schema-known file is parsed and
# run through its pydantic model, and parse failures are reported as ERRORs
# carrying the filename and the pydantic/YAML message.
#
# Schema-known files and their models (from context-story-64-4.md):
#   archetypes.yaml      → NpcArchetype       (requires name + description; extra="allow")
#   tropes.yaml          → TropeDefinition    (requires name; extra="forbid")
#   portrait_manifest.yaml → PortraitManifestEntry (requires name; extra="ignore";
#                            accepts {characters: [...]} AND bare-list shapes)
#
# tropes.yaml / archetypes.yaml are parsed as YAML LISTS of dicts (see
# loader._load_single_world l.779-791). An empty file parses to ``None`` →
# not a list → skipped, so the empty-file fixtures in TestPresenceGate stay
# valid after this pass is added. A list with a bad entry is the malformed case.
# ---------------------------------------------------------------------------


def _valid_pack_with_world(tmp_path: Path) -> Path:
    """Build a structurally complete pack with one complete, non-draft world.

    All schema-known content files (tropes/archetypes/portrait_manifest) are
    left empty (``.touch()`` from the helpers) — empty parses to ``None`` and
    is skipped by the content pass — so this baseline must report ZERO errors
    both before and after the content-validation pass is added. Individual
    tests overwrite exactly one schema-known file to isolate the failure.
    """
    pack_dir = tmp_path / "content_pack"
    pack_dir.mkdir()
    _minimal_pack(pack_dir)
    world_dir = pack_dir / "worlds" / "test_world"
    world_dir.mkdir(parents=True)
    _minimal_world(world_dir)
    return pack_dir


class TestContentValidation:
    # --- AC2: the headline regression — malformed content must FAIL not PASS ---

    def test_malformed_world_tropes_yaml_fails(self, tmp_path: Path) -> None:
        """A world tropes.yaml whose entry is missing the required ``name``
        produces a FAIL mentioning the file — this is the exact regression the
        story exists to prevent (it currently PASSES because contents are never
        parsed)."""
        pack_dir = _valid_pack_with_world(tmp_path)

        # Control: the baseline (empty tropes.yaml) must pass.
        baseline_errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert baseline_errors == [], (
            f"Baseline should pass before malforming content, got: {baseline_errors}"
        )

        # TropeDefinition requires `name`; this entry omits it.
        tropes_path = pack_dir / "worlds" / "test_world" / "tropes.yaml"
        tropes_path.write_text(
            yaml.dump([{"description": "a trope with no name"}]), encoding="utf-8"
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path_real)

        assert any("tropes.yaml" in e for e in errors), (
            f"Expected an ERROR naming 'tropes.yaml' for malformed content, got errors: {errors}"
        )

    def test_malformed_world_archetypes_yaml_fails_with_pydantic_message(
        self, tmp_path: Path
    ) -> None:
        """A world archetypes.yaml entry missing required fields fails, and the
        error carries the filename AND the pydantic message (names the field)."""
        pack_dir = _valid_pack_with_world(tmp_path)

        # NpcArchetype requires both `name` and `description`; this omits both.
        arch_path = pack_dir / "worlds" / "test_world" / "archetypes.yaml"
        arch_path.write_text(yaml.dump([{"personality_traits": ["gruff"]}]), encoding="utf-8")

        errors, _ = validate_pack_structure(pack_dir, schema_path_real)

        matching = [e for e in errors if "archetypes.yaml" in e]
        assert matching, f"Expected an ERROR naming 'archetypes.yaml', got errors: {errors}"
        # The pydantic message must be carried through, not just "invalid".
        joined = " ".join(matching).lower()
        assert "name" in joined or "description" in joined or "required" in joined, (
            f"Error should carry the pydantic message naming the missing field, got: {matching}"
        )

    def test_genre_tropes_extra_field_fails(self, tmp_path: Path) -> None:
        """Genre-tier tropes.yaml is validated too: TropeDefinition forbids
        extra fields, so an unknown key produces a FAIL. Guards that the pass
        runs at the pack tier, not only the world tier."""
        pack_dir = _valid_pack_with_world(tmp_path)

        genre_tropes = pack_dir / "tropes.yaml"
        genre_tropes.write_text(
            yaml.dump([{"name": "Betrayal", "bogus_field": 123}]), encoding="utf-8"
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path_real)

        assert any("tropes.yaml" in e for e in errors), (
            f"Expected genre-tier tropes.yaml content error, got: {errors}"
        )

    # --- AC1 edge: portrait_manifest accepts BOTH shapes (must not false-fail) ---

    def test_valid_portrait_manifest_characters_shape_passes(self, tmp_path: Path) -> None:
        """portrait_manifest.yaml in ``{characters: [...]}`` form with valid
        entries adds no error."""
        pack_dir = _valid_pack_with_world(tmp_path)

        manifest = pack_dir / "worlds" / "test_world" / "portrait_manifest.yaml"
        manifest.write_text(
            yaml.dump({"characters": [{"name": "Aldo", "role": "sheriff"}]}),
            encoding="utf-8",
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path_real)

        assert not any("portrait_manifest.yaml" in e for e in errors), (
            f"Valid {{characters: [...]}} manifest must not error, got: {errors}"
        )

    def test_valid_portrait_manifest_bare_list_shape_passes(self, tmp_path: Path) -> None:
        """portrait_manifest.yaml in bare-list form with valid entries adds no
        error."""
        pack_dir = _valid_pack_with_world(tmp_path)

        manifest = pack_dir / "worlds" / "test_world" / "portrait_manifest.yaml"
        manifest.write_text(yaml.dump([{"name": "Aldo", "role": "sheriff"}]), encoding="utf-8")

        errors, _ = validate_pack_structure(pack_dir, schema_path_real)

        assert not any("portrait_manifest.yaml" in e for e in errors), (
            f"Valid bare-list manifest must not error, got: {errors}"
        )

    def test_malformed_portrait_manifest_fails(self, tmp_path: Path) -> None:
        """portrait_manifest.yaml entry missing required ``name`` produces a
        FAIL mentioning the file."""
        pack_dir = _valid_pack_with_world(tmp_path)

        manifest = pack_dir / "worlds" / "test_world" / "portrait_manifest.yaml"
        manifest.write_text(yaml.dump([{"role": "sheriff"}]), encoding="utf-8")

        errors, _ = validate_pack_structure(pack_dir, schema_path_real)

        assert any("portrait_manifest.yaml" in e for e in errors), (
            f"Expected an ERROR naming 'portrait_manifest.yaml', got: {errors}"
        )

    # --- AC3: world.yaml parse errors reported loudly (pack.py:203-204) ---

    def test_invalid_world_yaml_reported_loudly(self, tmp_path: Path) -> None:
        """Invalid YAML in world.yaml must be reported as an ERROR naming the
        file — not silently swallowed by the bare ``except ... : pass`` at
        pack.py:203-204."""
        pack_dir = _valid_pack_with_world(tmp_path)

        world_yaml = pack_dir / "worlds" / "test_world" / "world.yaml"
        # Unbalanced flow sequence — yaml.safe_load raises YAMLError.
        world_yaml.write_text("name: broken\nbad: [unclosed\n", encoding="utf-8")

        errors, _ = validate_pack_structure(pack_dir, schema_path_real)

        assert any("world.yaml" in e for e in errors), (
            f"Invalid world.yaml must be reported loudly (no silent fallback), got errors: {errors}"
        )

    # --- Control / edge: empty schema-known files are NOT errors ---

    def test_empty_schema_known_file_is_not_error(self, tmp_path: Path) -> None:
        """An empty (``None``-parsing) tropes.yaml is skipped, not flagged —
        guards that the content pass doesn't regress draft/empty worlds or the
        live packs (which carry empty optional content files)."""
        pack_dir = _valid_pack_with_world(tmp_path)

        # Explicitly write an empty document (parses to None).
        (pack_dir / "worlds" / "test_world" / "tropes.yaml").write_text("", encoding="utf-8")

        errors, _ = validate_pack_structure(pack_dir, schema_path_real)

        assert errors == [], f"Empty schema-known file must not produce an error, got: {errors}"

    # --- AC4: real-content smoke + wiring test ---

    def test_all_live_packs_pass_content_validation(self) -> None:
        """Run the validator over every shipped genre pack: zero errors across
        all of them. This is the regression guard against the new content pass
        falsely rejecting shipped content, AND the wiring test that exercises
        validate_pack_structure against real production content.
        """
        genre_packs_root = Path(__file__).resolve().parents[4] / "sidequest-content" / "genre_packs"
        if not genre_packs_root.is_dir():
            pytest.skip(f"genre_packs root not found at {genre_packs_root}")

        pack_dirs = [
            p
            for p in sorted(genre_packs_root.iterdir())
            if p.is_dir() and not p.name.startswith(".") and (p / "pack.yaml").is_file()
        ]
        assert pack_dirs, f"No genre packs discovered under {genre_packs_root}"

        failures: dict[str, list[str]] = {}
        for pack in pack_dirs:
            errors, _ = validate_pack_structure(pack, schema_path_real)
            if errors:
                failures[pack.name] = errors

        assert not failures, (
            f"Live packs must pass content validation, but these failed: {failures}"
        )


class TestMapTreatmentValidation:
    """Story 163-3 / plan Task 8: a ``raster`` main-map treatment must declare a
    non-empty ``image``, a four-key ``provenance`` block, and a ``node_anchor``
    for every ``cartography.yaml`` region. Absent ``map.yaml`` is the dag
    fallback and must stay OK; non-raster treatments get a structural check only.

    All fixtures are synthetic (server tests never read live packs). These are
    black-box tests through ``validate_pack_structure`` — they also serve as the
    wiring test: a validator that is written but not called from
    ``_validate_world`` leaves these red.
    """

    def test_raster_map_missing_anchor_is_error(self, tmp_path: Path) -> None:
        """Every cartography region needs a node_anchor; a region (r2) with no
        anchor is an error."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n"
            "  r2: {name: R2, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text(
            "treatment: raster\nimage: sheet.jpg\n"
            "provenance: {source: OS, date: '1900', archive: NLS, pd_basis: expired}\n"
            "node_anchors:\n  r1: [1, 2]\n",  # r2 missing
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("r2" in e and "anchor" in e.lower() for e in errors), errors

    def test_raster_map_missing_provenance_is_error(self, tmp_path: Path) -> None:
        """A raster treatment with no provenance block at all is an error."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text(
            "treatment: raster\nimage: sheet.jpg\nnode_anchors:\n  r1: [1, 2]\n",
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("provenance" in e.lower() for e in errors), errors

    def test_raster_map_partial_provenance_missing_key_is_error(self, tmp_path: Path) -> None:
        """A provenance block present but missing ONE of the four required keys
        (here ``pd_basis``) is an error — exercises the per-key check, a distinct
        branch from a wholly absent provenance block."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text(
            "treatment: raster\nimage: sheet.jpg\n"
            "provenance: {source: OS, date: '1900', archive: NLS}\n"  # pd_basis missing
            "node_anchors:\n  r1: [1, 2]\n",
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("pd_basis" in e for e in errors), errors

    def test_raster_map_missing_image_is_error(self, tmp_path: Path) -> None:
        """A raster treatment with no ``image`` is an error — the map has nothing
        to render. Otherwise fully valid (provenance + anchors present) so the
        image rule is isolated."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text(
            "treatment: raster\n"  # no image
            "provenance: {source: OS, date: '1900', archive: NLS, pd_basis: expired}\n"
            "node_anchors:\n  r1: [1, 2]\n",
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("image" in e.lower() for e in errors), errors

    def test_unknown_treatment_kind_is_error(self, tmp_path: Path) -> None:
        """An unrecognised treatment kind is a structural error regardless of the
        raster-specific rules."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text("treatment: potato\n", encoding="utf-8")
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("potato" in e and "treatment" in e.lower() for e in errors), errors

    def test_non_raster_treatment_skips_raster_rules(self, tmp_path: Path) -> None:
        """A valid non-raster treatment (orrery) with no provenance and no anchors
        is OK — the raster-only rules must not leak onto other kinds. Control that
        guards over-application."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n"
            "  r2: {name: R2, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text("treatment: orrery\n", encoding="utf-8")
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert not any("provenance" in e.lower() or "anchor" in e.lower() for e in errors), errors

    def test_absent_map_yaml_is_ok(self, tmp_path: Path) -> None:
        """No ``map.yaml`` at all is the dag fallback — never an error. Control
        that guards the live packs (none ship a map.yaml today)."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)  # writes an empty cartography.yaml, no map.yaml
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert not any("map.yaml" in e for e in errors), errors

    def test_raster_map_non_dict_cartography_does_not_crash(self, tmp_path: Path) -> None:
        """A malformed (non-dict) cartography.yaml must not crash the validator —
        parity with `_validate_weather_zones`'s dict guard. The raster image/
        provenance rules still evaluate; anchor coverage is skipped because there
        are no readable regions in a non-mapping cartography."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        # cartography.yaml parses to a list, not a mapping.
        (world_dir / "cartography.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        (world_dir / "map.yaml").write_text(
            "treatment: raster\nimage: sheet.jpg\n"
            "provenance: {source: OS, date: '1900', archive: NLS, pd_basis: expired}\n"
            "node_anchors:\n  r1: [1, 2]\n",
            encoding="utf-8",
        )
        # Must not raise; a valid raster block over an unreadable cartography
        # yields no anchor-coverage errors (no regions to check).
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert not any("node_anchor" in e for e in errors), errors

    def test_raster_map_list_node_anchors_not_silently_covered(self, tmp_path: Path) -> None:
        """A bare-list `node_anchors` (region ids with no coordinates) must NOT
        count as anchor coverage — a list-membership check would falsely mark a
        region 'anchored'. Every region still needs a real mapping anchor."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text(
            "treatment: raster\nimage: sheet.jpg\n"
            "provenance: {source: OS, date: '1900', archive: NLS, pd_basis: expired}\n"
            "node_anchors: [r1]\n",  # a bare list, not a mapping of id -> coords
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("r1" in e and "anchor" in e.lower() for e in errors), errors

    def test_non_hashable_treatment_does_not_crash(self, tmp_path: Path) -> None:
        """A non-hashable `treatment` (e.g. a YAML list `treatment: [raster]`)
        must be reported as an unknown treatment, not crash the validator on the
        `kind not in {…}` set-membership check (TypeError: unhashable type)."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text("treatment: [raster]\n", encoding="utf-8")
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("treatment" in e.lower() for e in errors), errors

    def test_raster_map_whitespace_image_is_error(self, tmp_path: Path) -> None:
        """A whitespace-only `image` must not satisfy the non-empty-image gate."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text(
            'treatment: raster\nimage: "   "\n'
            "provenance: {source: OS, date: '1900', archive: NLS, pd_basis: expired}\n"
            "node_anchors:\n  r1: [1, 2]\n",
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("image" in e.lower() for e in errors), errors

    def test_raster_map_whitespace_provenance_is_error(self, tmp_path: Path) -> None:
        """A whitespace-only provenance value must not satisfy the PD-provenance
        gate — the licensing invariant Task 8 encodes cannot be defeated by
        blank-looking text."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n",
            encoding="utf-8",
        )
        (world_dir / "map.yaml").write_text(
            "treatment: raster\nimage: sheet.jpg\n"
            "provenance: {source: \"   \", date: '1900', archive: NLS, pd_basis: expired}\n"
            "node_anchors:\n  r1: [1, 2]\n",
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("source" in e for e in errors), errors


class TestWeatherZoneValidation:
    """Story 163-3 / plan Task 18: every region ``weather_zone`` must resolve to a
    key in the world's ``weather.yaml`` ``climate_zones``. A region declaring a
    weather_zone with no weather.yaml is an error (a climate binding with no
    climate). No weather_zone anywhere is OK.

    Synthetic fixtures only. ``cartography.yaml`` is not model-validated by the
    pack validator, so an unknown ``weather_zone`` field yields no competing error
    — the only source of a weather_zone error is ``_validate_weather_zones``.
    """

    def test_region_weather_zone_unknown_is_error(self, tmp_path: Path) -> None:
        """A region weather_zone that is not a declared climate zone is an error
        naming the bad zone."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d, weather_zone: nope}\n",
            encoding="utf-8",
        )
        (world_dir / "weather.yaml").write_text(
            "climate_zones:\n  glen_floor:\n    seasons:\n      autumn:\n"
            "        temp_range: [5, 12]\n        conditions: [smirr]\n        weights: [1]\n",
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("weather_zone" in e and "nope" in e for e in errors), errors

    def test_region_weather_zone_valid_passes(self, tmp_path: Path) -> None:
        """A region weather_zone that IS a declared climate zone is OK — control
        against false-failing valid content."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d, weather_zone: glen_floor}\n",
            encoding="utf-8",
        )
        (world_dir / "weather.yaml").write_text(
            "climate_zones:\n  glen_floor:\n    seasons:\n      autumn:\n"
            "        temp_range: [5, 12]\n        conditions: [smirr]\n        weights: [1]\n",
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert not any("weather_zone" in e for e in errors), errors

    def test_weather_zone_declared_without_weather_yaml_is_error(self, tmp_path: Path) -> None:
        """A region declaring a weather_zone with NO ``weather.yaml`` present is an
        error — a climate binding with nothing to bind to. This also guards the
        absent-file read path: the validator must NOT crash on the missing
        weather.yaml (``_read_yaml`` raises FileNotFoundError without an
        ``is_file()`` guard)."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d, weather_zone: glen_floor}\n",
            encoding="utf-8",
        )
        # deliberately NO weather.yaml
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("weather_zone" in e for e in errors), errors

    def test_no_weather_zone_anywhere_is_ok(self, tmp_path: Path) -> None:
        """A world whose regions declare no weather_zone (and which has no
        weather.yaml) produces no weather errors — control for the early return."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d}\n",
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert not any("weather" in e.lower() for e in errors), errors

    def test_non_hashable_weather_zone_does_not_crash(self, tmp_path: Path) -> None:
        """A non-hashable region `weather_zone` (e.g. `weather_zone: [glen_floor]`)
        must be reported as an invalid zone, not crash the validator on the
        `wz not in zones` set-membership check (TypeError: unhashable type)."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d, weather_zone: [glen_floor]}\n",
            encoding="utf-8",
        )
        (world_dir / "weather.yaml").write_text(
            "climate_zones:\n  glen_floor:\n    seasons:\n      autumn:\n"
            "        temp_range: [5, 12]\n        conditions: [smirr]\n        weights: [1]\n",
            encoding="utf-8",
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("weather_zone" in e for e in errors), errors

    def test_malformed_climate_zones_scalar_does_not_crash(self, tmp_path: Path) -> None:
        """A scalar `climate_zones` (e.g. `climate_zones: 42`) must be reported,
        not crash the validator on `set(42)` (TypeError: not iterable)."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d, weather_zone: glen_floor}\n",
            encoding="utf-8",
        )
        (world_dir / "weather.yaml").write_text("climate_zones: 42\n", encoding="utf-8")
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        # A clean error string is produced (no crash); it names the climate binding.
        assert any("climate" in e.lower() for e in errors), errors

    def test_malformed_climate_zones_list_of_dicts_does_not_crash(self, tmp_path: Path) -> None:
        """A list-of-mappings `climate_zones` must be reported, not crash the
        validator on `set([{...}])` (TypeError: unhashable dict)."""
        pack_dir = tmp_path / "p"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        world_dir = pack_dir / "worlds" / "w"
        world_dir.mkdir(parents=True)
        _minimal_world(world_dir)
        (world_dir / "cartography.yaml").write_text(
            "navigation_mode: region\nstarting_region: r1\n"
            "regions:\n  r1: {name: R1, summary: s, description: d, weather_zone: glen_floor}\n",
            encoding="utf-8",
        )
        (world_dir / "weather.yaml").write_text(
            "climate_zones:\n  - {name: glen_floor}\n", encoding="utf-8"
        )
        errors, _ = validate_pack_structure(pack_dir, schema_path_real)
        assert any("climate" in e.lower() for e in errors), errors
