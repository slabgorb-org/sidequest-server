"""RED tests for Story 64-5 — cross-reference content lint.

Builds on 64-4 (``tests/cli/validate/test_pack_validator.py``). 64-4 proved each
file PARSES through its leaf pydantic model. This story adds a CROSS-reference
lint layer that validates references the per-file models can't see on their own:

  AC1  trope IDs referenced in history.yaml chapters AND legends' related_tropes
       must exist in the resolved trope set (genre tropes.yaml ∪ world tropes.yaml).
  AC2  archetype typical_classes / typical_races must be in rules.yaml's
       allowed_classes / allowed_races.
  AC3  archetype_constraints valid_pairings jungian/role IDs must be from the
       canonical sets defined in the repo-global archetypes_base.yaml (parsed via
       the BaseArchetypes leaf model), and genre_flavor must cover EXACTLY those
       jungian + rpg_role ids — no missing, no extra. (npc_roles_available is a
       SEPARATE field against the separate npc_roles set — not conflated here.)
  AC4  theme palette adjacency closure (prefers/avoids ids exist) — ALREADY
       enforced by load_theme_palette via _validate_theme_palette; pinned here.
  AC5  all 10 live packs still PASS (wiring + regression guard).

Per project rule, failure-case fixtures are synthesised in ``tmp_path`` — no
iteration over live content for the failure cases. The single exception is the
AC5 wiring/regression test, which deliberately runs the real validator over
shipped packs (the CLAUDE.md-mandated end-to-end wiring test).

Fixture layout (a realistic content root so archetypes_base.yaml resolves the
way ``_find_default_schema`` discovers ``pack_schema.yaml`` — walk up from the
pack dir):

    tmp_path/sidequest-content/pack_schema.yaml       (copy of real)
    tmp_path/sidequest-content/archetypes_base.yaml   (copy of real)
    tmp_path/sidequest-content/genre_packs/test_pack/...
    tmp_path/sidequest-content/genre_packs/test_pack/worlds/test_world/...

The canonical jungian/role IDs are DERIVED by parsing the copied
archetypes_base.yaml — not hard-coded — so the tests assert behaviour (known-bad
ids ERROR, full coverage PASSES) rather than pinning the 12+7 literals.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sidequest.cli.validate.pack import load_pack_schema, validate_pack_structure
from sidequest.genre.models.archetype_axes import BaseArchetypes

# ---------------------------------------------------------------------------
# Real-content anchors (four levels up: tests/cli/validate → tests/cli → tests →
# sidequest-server → oq-2 ... then into sidequest-content)
# ---------------------------------------------------------------------------

_REAL_CONTENT = Path(__file__).resolve().parents[4] / "sidequest-content"
_REAL_SCHEMA = _REAL_CONTENT / "pack_schema.yaml"
_REAL_BASE = _REAL_CONTENT / "archetypes_base.yaml"
_REAL_GENRE_PACKS = _REAL_CONTENT / "genre_packs"
_REAL_BONE_CRYPT = _REAL_CONTENT / "genre_packs" / "caverns_and_claudes" / "themes" / "bone_crypt.yaml"


def _require(path: Path) -> None:
    if not path.is_file():
        pytest.skip(f"required real-content anchor not found: {path}")


# ---------------------------------------------------------------------------
# Fixture builders — minimal pack/world derived from the real schema (no
# hard-coded required-file lists, so this never drifts from pack_schema.yaml).
# ---------------------------------------------------------------------------


def _touch_all(base: Path, files: list[str], dirs: list[str]) -> None:
    for fname in files:
        fpath = base / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.touch()
    for dname in dirs:
        (base / dname).mkdir(parents=True, exist_ok=True)


def _build_pack(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a structurally complete, content-empty pack inside a realistic
    ``sidequest-content`` root. Returns ``(schema_path, pack_dir, world_dir)``.

    All schema-known content files are empty (``None``-parsing) so the baseline
    reports ZERO errors both before and after the cross-ref pass is added.
    Individual tests overwrite exactly one file to isolate a single failure.
    """
    _require(_REAL_SCHEMA)
    _require(_REAL_BASE)

    content_root = tmp_path / "sidequest-content"
    (content_root / "genre_packs").mkdir(parents=True)
    shutil.copy(_REAL_SCHEMA, content_root / "pack_schema.yaml")
    shutil.copy(_REAL_BASE, content_root / "archetypes_base.yaml")
    schema_path = content_root / "pack_schema.yaml"

    schema = load_pack_schema(schema_path)
    genre = schema.get("genre_pack", {})
    world = schema.get("world", {})

    pack_dir = content_root / "genre_packs" / "test_pack"
    pack_dir.mkdir()
    _touch_all(pack_dir, genre.get("required_files", []), genre.get("required_dirs", []))

    world_dir = pack_dir / "worlds" / "test_world"
    world_dir.mkdir(parents=True)
    _touch_all(world_dir, world.get("required_files", []), world.get("required_dirs", []))

    return schema_path, pack_dir, world_dir


def _write_yaml(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data), encoding="utf-8")


def _base_archetypes() -> BaseArchetypes:
    _require(_REAL_BASE)
    return BaseArchetypes.model_validate(yaml.safe_load(_REAL_BASE.read_text(encoding="utf-8")))


def _valid_constraints(base: BaseArchetypes) -> dict:
    """A structurally-valid, fully-canonical archetype_constraints mapping.

    valid_pairings reference only canonical ids; genre_flavor covers EXACTLY the
    canonical jungian + rpg_role sets; npc_roles_available is the canonical
    npc_role set. Tests mutate one slice of this to isolate a single violation.
    """
    jids = [j.id for j in base.jungian]
    rids = [r.id for r in base.rpg_roles]
    nids = [n.id for n in base.npc_roles]
    return {
        "valid_pairings": {
            "common": [[jids[0], rids[0]], [jids[1], rids[1]]],
            "uncommon": [],
            "rare": [],
            "forbidden": [],
        },
        "genre_flavor": {
            "jungian": {j: {} for j in jids},
            "rpg_roles": {r: {"fallback_name": f"Flavor {r}"} for r in rids},
        },
        "npc_roles_available": list(nids),
    }


# ===========================================================================
# AC1 — trope ID membership (history chapters + legends related_tropes)
# ===========================================================================


class TestTropeIdMembership:
    def test_baseline_empty_history_passes(self, tmp_path: Path) -> None:
        """Control: the all-empty pack reports zero errors before any malforming.
        Guards that the cross-ref pass does not regress empty/optional files."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)
        errors, _ = validate_pack_structure(pack_dir, schema_path)
        assert errors == [], f"Baseline must pass, got: {errors}"

    def test_history_chapter_trope_id_not_in_set_is_error(self, tmp_path: Path) -> None:
        """A trope id referenced in a history chapter's ``tropes:`` block that is
        absent from the resolved trope set → ERROR naming the id AND the file."""
        schema_path, _pack_dir, world_dir = _build_pack(tmp_path)
        pack_dir = world_dir.parents[1]

        _write_yaml(world_dir / "tropes.yaml", [{"id": "real_trope", "name": "Real Trope"}])
        # beneath_sunden-style: top-level chapters, each with a nested tropes block.
        _write_yaml(
            world_dir / "history.yaml",
            {"chapters": [{"id": "fresh", "tropes": [{"id": "ghost_trope"}]}]},
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        offenders = [e for e in errors if "ghost_trope" in e]
        assert offenders, f"Expected an ERROR naming the unresolvable trope id, got: {errors}"
        assert any("history.yaml" in e for e in offenders), (
            f"The trope-id error must name the offending file (history.yaml), got: {offenders}"
        )

    def test_history_chapter_trope_id_in_set_passes(self, tmp_path: Path) -> None:
        """A history trope ref that DOES resolve produces no history error
        (guards against false positives on valid refs)."""
        schema_path, _pack_dir, world_dir = _build_pack(tmp_path)
        pack_dir = world_dir.parents[1]

        _write_yaml(world_dir / "tropes.yaml", [{"id": "real_trope", "name": "Real Trope"}])
        _write_yaml(
            world_dir / "history.yaml",
            {"chapters": [{"id": "fresh", "tropes": [{"id": "real_trope"}]}]},
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert not any("history.yaml" in e for e in errors), (
            f"A resolvable history trope ref must not error, got: {errors}"
        )

    def test_resolved_set_unions_genre_and_world_tropes(self, tmp_path: Path) -> None:
        """A world history ref to a trope defined only in the GENRE-tier
        tropes.yaml resolves — the resolved set is the UNION of genre + world."""
        schema_path, _pack_dir, world_dir = _build_pack(tmp_path)
        pack_dir = world_dir.parents[1]

        # Trope lives at genre tier; world tropes.yaml stays empty.
        _write_yaml(pack_dir / "tropes.yaml", [{"id": "genre_trope", "name": "Genre Trope"}])
        _write_yaml(
            world_dir / "history.yaml",
            {"chapters": [{"id": "fresh", "tropes": [{"id": "genre_trope"}]}]},
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert not any("history.yaml" in e for e in errors), (
            f"A genre-defined trope must resolve a world history ref, got: {errors}"
        )

    def test_legend_related_trope_not_in_set_is_error(self, tmp_path: Path) -> None:
        """A trope id in a legend's ``related_tropes`` that is absent from the
        resolved trope set → ERROR naming the id."""
        schema_path, _pack_dir, world_dir = _build_pack(tmp_path)
        pack_dir = world_dir.parents[1]

        _write_yaml(world_dir / "tropes.yaml", [{"id": "real_trope", "name": "Real Trope"}])
        # legends/ is a required world dir (created empty by the builder); add a file.
        _write_yaml(
            world_dir / "legends" / "the_phantom.yaml",
            {"name": "The Phantom", "summary": "A ghost story.", "related_tropes": ["phantom_trope"]},
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert any("phantom_trope" in e for e in errors), (
            f"Expected an ERROR naming the unresolvable legend related_trope, got: {errors}"
        )


# ===========================================================================
# AC2 — archetype typical_classes / typical_races vs rules.yaml allowed sets
# ===========================================================================


class TestArchetypeClassRaceMembership:
    def test_typical_class_not_allowed_is_error(self, tmp_path: Path) -> None:
        """An archetype naming a class absent from rules.yaml allowed_classes →
        ERROR naming the offending class AND the file."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)

        _write_yaml(
            pack_dir / "rules.yaml",
            {"allowed_classes": ["Gunslinger", "Marshal"], "allowed_races": ["Frontier Born"]},
        )
        _write_yaml(
            pack_dir / "archetypes.yaml",
            [{"name": "The Stranger", "description": "A drifter.", "typical_classes": ["Wizard"]}],
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        offenders = [e for e in errors if "Wizard" in e]
        assert offenders, f"Expected an ERROR naming the disallowed class 'Wizard', got: {errors}"
        assert any("archetypes.yaml" in e for e in offenders), (
            f"The class-membership error must name the file (archetypes.yaml), got: {offenders}"
        )

    def test_typical_race_not_allowed_is_error(self, tmp_path: Path) -> None:
        """An archetype naming a race absent from rules.yaml allowed_races →
        ERROR naming the offending race."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)

        _write_yaml(
            pack_dir / "rules.yaml",
            {"allowed_classes": ["Gunslinger"], "allowed_races": ["Frontier Born", "City Exile"]},
        )
        _write_yaml(
            pack_dir / "archetypes.yaml",
            [{"name": "The Stranger", "description": "A drifter.", "typical_races": ["Elf"]}],
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert any("Elf" in e for e in errors), (
            f"Expected an ERROR naming the disallowed race 'Elf', got: {errors}"
        )

    def test_typical_class_and_race_allowed_passes(self, tmp_path: Path) -> None:
        """Archetype whose class AND race are both in the allowed sets produces
        no archetypes error (guards against false positives)."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)

        _write_yaml(
            pack_dir / "rules.yaml",
            {"allowed_classes": ["Gunslinger"], "allowed_races": ["Frontier Born"]},
        )
        _write_yaml(
            pack_dir / "archetypes.yaml",
            [
                {
                    "name": "The Stranger",
                    "description": "A drifter.",
                    "typical_classes": ["Gunslinger"],
                    "typical_races": ["Frontier Born"],
                }
            ],
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert not any("archetypes.yaml" in e for e in errors), (
            f"Valid class/race refs must not error, got: {errors}"
        )


# ===========================================================================
# AC3 — archetype_constraints canonical jungian/role IDs + genre_flavor coverage
# ===========================================================================


class TestArchetypeConstraintsCrossRef:
    def test_fully_canonical_constraints_pass(self, tmp_path: Path) -> None:
        """Control: constraints whose pairings use only canonical ids and whose
        genre_flavor covers exactly the canonical jungian + role sets pass."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)
        base = _base_archetypes()

        _write_yaml(pack_dir / "archetype_constraints.yaml", _valid_constraints(base))

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert not any("archetype_constraints.yaml" in e for e in errors), (
            f"Fully-canonical constraints must not error, got: {errors}"
        )

    def test_non_canonical_jungian_in_pairing_is_error(self, tmp_path: Path) -> None:
        """A valid_pairings entry naming a jungian id not in archetypes_base →
        ERROR naming the id AND the file."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)
        base = _base_archetypes()
        constraints = _valid_constraints(base)
        rid = base.rpg_roles[0].id
        constraints["valid_pairings"]["common"].append(["villain", rid])

        _write_yaml(pack_dir / "archetype_constraints.yaml", constraints)

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        offenders = [e for e in errors if "villain" in e]
        assert offenders, f"Expected an ERROR naming the non-canonical jungian 'villain', got: {errors}"
        assert any("archetype_constraints.yaml" in e for e in offenders), (
            f"Error must name the file (archetype_constraints.yaml), got: {offenders}"
        )

    def test_non_canonical_role_in_pairing_is_error(self, tmp_path: Path) -> None:
        """A valid_pairings entry naming an rpg_role id not in archetypes_base →
        ERROR naming the id."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)
        base = _base_archetypes()
        constraints = _valid_constraints(base)
        jid = base.jungian[0].id
        constraints["valid_pairings"]["common"].append([jid, "brawler"])

        _write_yaml(pack_dir / "archetype_constraints.yaml", constraints)

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert any("brawler" in e for e in errors), (
            f"Expected an ERROR naming the non-canonical role 'brawler', got: {errors}"
        )

    def test_missing_genre_flavor_jungian_entry_is_error(self, tmp_path: Path) -> None:
        """genre_flavor that omits a canonical jungian → ERROR naming the missing
        id (coverage must be complete — these were hand-audited in epic 64)."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)
        base = _base_archetypes()
        constraints = _valid_constraints(base)
        dropped = base.jungian[0].id
        del constraints["genre_flavor"]["jungian"][dropped]

        _write_yaml(pack_dir / "archetype_constraints.yaml", constraints)

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        offenders = [e for e in errors if dropped in e]
        assert offenders, (
            f"Expected an ERROR naming the missing genre_flavor jungian {dropped!r}, got: {errors}"
        )
        assert any("archetype_constraints.yaml" in e for e in offenders), (
            f"Error must name the file (archetype_constraints.yaml), got: {offenders}"
        )

    def test_extra_genre_flavor_jungian_entry_is_error(self, tmp_path: Path) -> None:
        """genre_flavor with a jungian key NOT in the canonical set → ERROR
        naming the extra id (no extras allowed)."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)
        base = _base_archetypes()
        constraints = _valid_constraints(base)
        constraints["genre_flavor"]["jungian"]["trickster"] = {}

        _write_yaml(pack_dir / "archetype_constraints.yaml", constraints)

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert any("trickster" in e for e in errors), (
            f"Expected an ERROR naming the extra genre_flavor jungian 'trickster', got: {errors}"
        )


# ===========================================================================
# AC4 — theme palette adjacency closure (ALREADY enforced; pinned here)
# ===========================================================================


class TestThemeAdjacencyClosure:
    def _write_theme(self, themes_dir: Path, fname: str, mutate) -> None:
        """Copy the real bone_crypt theme (a known-valid DungeonTheme) and apply
        ``mutate`` to the parsed dict before writing it as ``fname``."""
        _require(_REAL_BONE_CRYPT)
        data = yaml.safe_load(_REAL_BONE_CRYPT.read_text(encoding="utf-8"))
        mutate(data)
        _write_yaml(themes_dir / fname, data)

    def test_dangling_adjacency_ref_surfaces_as_error(self, tmp_path: Path) -> None:
        """A theme whose adjacency.prefers names a theme id not in the palette →
        validator ERROR (closure already enforced by load_theme_palette; this
        pins that it SURFACES through validate_pack_structure)."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)
        themes_dir = pack_dir / "themes"
        themes_dir.mkdir()

        def mutate(d: dict) -> None:
            d["id"] = "crypt_a"
            d["adjacency"] = {"prefers": ["ghost_theme"], "avoids": []}

        self._write_theme(themes_dir, "crypt_a.yaml", mutate)

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert any("ghost_theme" in e for e in errors), (
            f"Dangling theme adjacency ref must surface as a validator ERROR, got: {errors}"
        )

    def test_closed_adjacency_passes(self, tmp_path: Path) -> None:
        """A single theme with no dangling adjacency refs passes (guards against
        false positives in the closure check)."""
        schema_path, pack_dir, _world = _build_pack(tmp_path)
        themes_dir = pack_dir / "themes"
        themes_dir.mkdir()

        def mutate(d: dict) -> None:
            d["id"] = "crypt_a"
            d["adjacency"] = {"prefers": [], "avoids": []}

        self._write_theme(themes_dir, "crypt_a.yaml", mutate)

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert not any("themes/" in e for e in errors), (
            f"A closed theme palette must not error, got: {errors}"
        )


# ===========================================================================
# AC5 — wiring + regression: all live packs still PASS
# ===========================================================================


def test_all_live_packs_pass_cross_reference_lint() -> None:
    """Run the validator over every shipped genre pack: zero errors across all of
    them. This is BOTH the regression guard (the new cross-ref pass must not
    falsely reject hand-audited shipped content) AND the end-to-end wiring test
    that exercises validate_pack_structure against real production content.
    """
    if not _REAL_GENRE_PACKS.is_dir():
        pytest.skip(f"genre_packs root not found at {_REAL_GENRE_PACKS}")
    _require(_REAL_SCHEMA)

    pack_dirs = [
        p
        for p in sorted(_REAL_GENRE_PACKS.iterdir())
        if p.is_dir() and not p.name.startswith(".") and (p / "pack.yaml").is_file()
    ]
    assert pack_dirs, f"No genre packs discovered under {_REAL_GENRE_PACKS}"

    failures: dict[str, list[str]] = {}
    for pack in pack_dirs:
        errors, _ = validate_pack_structure(pack, _REAL_SCHEMA)
        if errors:
            failures[pack.name] = errors

    assert not failures, f"Live packs must pass cross-reference lint, but these failed: {failures}"


# ===========================================================================
# Silent-failure guard (Reviewer HIGH) — history.yaml / legends/*.yaml have NO
# pydantic model wired to them, so the ONLY reader is the cross-ref trope-ref
# pass. Today that pass swallows _read_yaml parse errors (returns []/continue),
# so a syntactically BROKEN history or legend file makes the validator report
# PASS — a silent failure (violates No Silent Fallbacks). A broken cross-ref
# SOURCE must surface as a loud ERROR naming the file, never a clean pass.
# ===========================================================================


class TestBrokenCrossRefSourcesAreLoud:
    def test_broken_history_yaml_is_error(self, tmp_path: Path) -> None:
        """A syntactically invalid world history.yaml must produce an ERROR
        naming the file — not a silent PASS. No pydantic model is wired to
        history.yaml, so the cross-ref pass is the only line of defence."""
        schema_path, _pack_dir, world_dir = _build_pack(tmp_path)
        pack_dir = world_dir.parents[1]

        # Unbalanced flow sequence — yaml.safe_load raises YAMLError.
        (world_dir / "history.yaml").write_text(
            "chapters:\n  - id: fresh\n    tropes: [unclosed\n", encoding="utf-8"
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert any("history.yaml" in e for e in errors), (
            f"A broken history.yaml must surface a loud ERROR naming the file "
            f"(No Silent Fallbacks), got: {errors}"
        )

    def test_broken_legend_yaml_is_error(self, tmp_path: Path) -> None:
        """A syntactically invalid legends/*.yaml must produce an ERROR naming
        the file — not a silent PASS. No pydantic model is wired to legend
        files in the validator, so the cross-ref pass is the only defence."""
        schema_path, _pack_dir, world_dir = _build_pack(tmp_path)
        pack_dir = world_dir.parents[1]

        # legends/ is a required world dir (created empty by the builder).
        (world_dir / "legends" / "broken_legend.yaml").write_text(
            "name: The Phantom\nrelated_tropes: [unclosed\n", encoding="utf-8"
        )

        errors, _ = validate_pack_structure(pack_dir, schema_path)

        assert any("broken_legend.yaml" in e for e in errors), (
            f"A broken legend file must surface a loud ERROR naming the file "
            f"(No Silent Fallbacks), got: {errors}"
        )
