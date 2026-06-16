"""Story 113-2 — reconcile pack_schema.yaml with the real loader allowlist.

The 2026-06-14 genre-pack-root audit found pack_schema.yaml and the genre-pack
loader (``sidequest/genre/loader.py``) drifted in BOTH directions, so the pack
validator could neither flag dead files nor recognize live ones:

  * SCHEMA-LISTS-but-loader-DROPPED (dead genre extensions):
      openings.yaml  — loader sets ``openings = []`` (world-tier only now)
      powers.yaml    — fully unreferenced in the server
      weather.yaml   — repointed to world_dir (Epic 74); pack-root copy unread

  * LOADER-READS-but-schema-OMITS (live mechanics, invisible to the validator):
      skills.yaml, spells_wwn.yaml, foci.yaml, bestiary.yaml, backgrounds.yaml,
      witnessed_acts.yaml, mutations.yaml, disciplines_psionic.yaml

Acceptance criteria (context-story-113-2.md):

  AC1  pack_schema.yaml genre extensions match the loader's actual filename
       allowlist: the three dead files removed, the eight live files added.
  AC2  a test asserts schema and loader allowlist agree, so future drift fails
       CI (the loader must expose its genre-pack-root allowlist as data — a
       single source of truth the schema is validated against).
  AC3  the pack validator flags (at least WARN) an unknown YAML at a pack root
       instead of silently ignoring it — AND must NOT false-flag a live file the
       loader reads (e.g. skills.yaml) as an orphan just because the pack did not
       declare it as an extension.

CONTRACT for the GREEN phase (AC2):
  ``sidequest.genre.loader`` must expose a module-level
  ``GENRE_PACK_ROOT_EXTENSION_FILES: frozenset[str]`` — every OPTIONAL
  (extension-tier) YAML filename the loader reads at the genre pack root. This is
  the loader's half of the single-source-of-truth allowlist. The schema's half is
  the union of ``genre_pack.extensions[*].files``. The two MUST be equal.
  (Dev may choose a different symbol name, but then this test's import must be
  updated to match — the *behavior* under test, schema/loader agreement, is
  fixed.)

Per project rule: server tests do NOT iterate over live content packs; all
fixtures are synthesised in ``tmp_path``. The one exception is reading the real
``pack_schema.yaml`` (the artifact under reconciliation), matching the existing
``test_pack_validator.py`` convention.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sidequest.cli.validate.pack import load_pack_schema, validate_pack_structure

# Four levels up: tests/cli/validate/ → tests/cli/ → tests/ → sidequest-server/
# → oq-2/ ... then into sidequest-content.
schema_path_real = Path(__file__).resolve().parents[4] / "sidequest-content" / "pack_schema.yaml"

# The three genre-tier extension files the loader DROPPED — must be removed from
# the schema's genre_pack.extensions (verified dead in loader.py: openings set to
# [] at the genre tier, powers.yaml unreferenced, weather.yaml repointed to the
# world dir).
DEAD_GENRE_EXTENSION_FILES = {"openings.yaml", "powers.yaml", "weather.yaml"}

# The eight live genre-tier files the loader READS but the schema OMITTED — must
# be added to the schema's genre_pack.extensions (each has a confirmed read-site
# at the genre pack root in loader.py).
LIVE_GENRE_EXTENSION_FILES = {
    "skills.yaml",
    "spells_wwn.yaml",
    "foci.yaml",
    "bestiary.yaml",
    "backgrounds.yaml",
    "witnessed_acts.yaml",
    "mutations.yaml",
    "disciplines_psionic.yaml",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_genre_extension_files(schema: dict[str, Any]) -> set[str]:
    """Collect every filename declared under ``genre_pack.extensions[*].files``."""
    files: set[str] = set()
    extensions = schema.get("genre_pack", {}).get("extensions", {})
    for spec in extensions.values():
        if not isinstance(spec, dict):
            continue
        for fname in spec.get("files", []) or []:
            files.add(str(fname))
    return files


def _minimal_pack(root: Path) -> Path:
    """Create a minimal genre-pack directory satisfying every required file/dir
    from the real pack_schema.yaml (mirrors test_pack_validator._minimal_pack).

    ``pack.yaml`` is created empty → no extensions are *declared*, which is the
    realistic case for the live-file-orphan check (live packs ship skills.yaml /
    bestiary.yaml etc. WITHOUT declaring them as extensions, because the loader
    reads them unconditionally).
    """
    required_files = [
        "pack.yaml",
        "theme.yaml",
        "archetypes.yaml",
        "tropes.yaml",
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
    required_dirs = ["audio/music", "assets/fonts", "worlds"]
    for fname in required_files:
        fpath = root / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.touch()
    for dname in required_dirs:
        (root / dname).mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# AC1 — schema reconciliation (pure data assertions against pack_schema.yaml)
# ---------------------------------------------------------------------------


class TestSchemaReconciliation:
    def test_dead_genre_extensions_removed(self) -> None:
        """openings/powers/weather.yaml must NOT appear in genre_pack.extensions."""
        schema = load_pack_schema(schema_path_real)
        ext_files = _schema_genre_extension_files(schema)
        leaked = DEAD_GENRE_EXTENSION_FILES & ext_files
        assert not leaked, (
            "pack_schema.yaml genre_pack.extensions still lists loader-dead files "
            f"{sorted(leaked)} — the loader no longer reads these at the genre tier "
            "(openings=[] at genre tier, powers.yaml unreferenced, weather.yaml "
            "repointed to the world dir). Remove their extensions from the schema."
        )

    def test_live_genre_extensions_added(self) -> None:
        """The eight loader-read files must ALL appear in genre_pack.extensions."""
        schema = load_pack_schema(schema_path_real)
        ext_files = _schema_genre_extension_files(schema)
        missing = LIVE_GENRE_EXTENSION_FILES - ext_files
        assert not missing, (
            "pack_schema.yaml genre_pack.extensions omits live genre-tier files "
            f"{sorted(missing)} that the loader actually reads — the validator "
            "cannot recognize them and false-flags them as orphans. Add them to "
            "the schema's genre_pack.extensions."
        )


# ---------------------------------------------------------------------------
# AC2 — schema/loader allowlist agreement (the drift guard)
#
# These import the (to-be-created) loader allowlist constant lazily INSIDE each
# test so a missing constant fails only THESE tests (RED for the right reason)
# rather than breaking collection of the whole module.
# ---------------------------------------------------------------------------


class TestSchemaLoaderAgreement:
    def test_loader_allowlist_excludes_dead_files(self) -> None:
        from sidequest.genre.loader import GENRE_PACK_ROOT_EXTENSION_FILES

        leaked = DEAD_GENRE_EXTENSION_FILES & set(GENRE_PACK_ROOT_EXTENSION_FILES)
        assert not leaked, (
            f"loader GENRE_PACK_ROOT_EXTENSION_FILES claims to read dead files "
            f"{sorted(leaked)} — the loader does not read these at the genre tier."
        )

    def test_loader_allowlist_includes_live_files(self) -> None:
        from sidequest.genre.loader import GENRE_PACK_ROOT_EXTENSION_FILES

        missing = LIVE_GENRE_EXTENSION_FILES - set(GENRE_PACK_ROOT_EXTENSION_FILES)
        assert not missing, (
            f"loader GENRE_PACK_ROOT_EXTENSION_FILES omits live genre-tier files "
            f"{sorted(missing)} that the loader reads at the genre pack root."
        )

    def test_schema_and_loader_allowlist_agree(self) -> None:
        """The drift guard: schema genre extensions == loader allowlist, exactly.

        This is the test that makes future drift fail CI. If it surfaces drift
        beyond the eleven audit-named files (a schema-listed extension the loader
        does not read at the genre tier, or a loader read the schema omits), that
        IS the 'drifted in both directions' the story exists to close — reconcile
        the offending file rather than weakening this assertion.
        """
        from sidequest.genre.loader import GENRE_PACK_ROOT_EXTENSION_FILES

        schema = load_pack_schema(schema_path_real)
        schema_files = _schema_genre_extension_files(schema)
        loader_files = set(GENRE_PACK_ROOT_EXTENSION_FILES)

        schema_only = schema_files - loader_files
        loader_only = loader_files - schema_files
        assert not schema_only and not loader_only, (
            "pack_schema.yaml genre extensions and the loader allowlist disagree.\n"
            f"  in schema but NOT read by loader (remove from schema): {sorted(schema_only)}\n"
            f"  read by loader but NOT in schema (add to schema):       {sorted(loader_only)}"
        )


# ---------------------------------------------------------------------------
# AC3 — validator recognizes live files / flags genuine unknowns
# ---------------------------------------------------------------------------


class TestValidatorUnknownFileHandling:
    def test_unknown_pack_root_yaml_is_flagged(self, tmp_path: Path) -> None:
        """Regression guard: a genuinely unknown YAML at the pack root WARNs
        (No-Silent-Fallbacks). This already holds via the orphan check — pin it
        so the reconciliation fix cannot accidentally silence genuine unknowns."""
        pack_dir = tmp_path / "guard_pack"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        (pack_dir / "totally_unknown_xyz.yaml").write_text("a: 1\n", encoding="utf-8")

        errors, warnings = validate_pack_structure(pack_dir, schema_path_real)
        flagged = any("totally_unknown_xyz.yaml" in m for m in (errors + warnings))
        assert flagged, (
            "an unknown YAML at the pack root was silently ignored — it must be "
            f"flagged (at least WARN). errors={errors} warnings={warnings}"
        )

    def test_live_genre_root_file_is_not_orphaned(self, tmp_path: Path) -> None:
        """A live genre-tier file the loader reads (skills.yaml) present at the
        pack root must NOT be flagged as an orphan, even when the pack does not
        declare a matching extension in pack.yaml — the validator recognizes the
        canonical allowlist, matching the loader. RED today: skills.yaml is absent
        from the schema AND the orphan check only treats DECLARED extensions as
        known, so a present-undeclared skills.yaml false-orphans."""
        pack_dir = tmp_path / "live_file_pack"
        pack_dir.mkdir()
        _minimal_pack(pack_dir)
        # A real, well-formed (empty-list) skills catalog at the genre root,
        # NOT declared as an extension in pack.yaml.
        (pack_dir / "skills.yaml").write_text("[]\n", encoding="utf-8")

        errors, warnings = validate_pack_structure(pack_dir, schema_path_real)
        orphaned = [m for m in (errors + warnings) if "skills.yaml" in m and "orphan" in m]
        assert not orphaned, (
            "a live genre-tier file the loader reads (skills.yaml) was flagged as "
            f"an orphan — the validator must recognize the canonical allowlist. "
            f"offending messages: {orphaned}"
        )
