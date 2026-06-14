"""``pf validate pack`` — genre pack directory structure validator.

Validates a genre pack directory against ``pack_schema.yaml``, checking:

1. Required files are present at the pack (genre) level.
2. Required directories are present at the pack level.
3. Declared extensions (in ``pack.yaml`` ``extensions`` list) have their
   required files/dirs.
4. Orphan files/dirs (present but not in schema) are flagged as warnings.
5. World directories under ``worlds/`` are validated recursively; worlds
   with ``draft: true`` in ``world.yaml`` receive warnings instead of errors.

Usage::

    python -m sidequest.cli.validate pack <pack_dir>
    python -m sidequest.cli.validate pack <genre_packs_root>
    python -m sidequest.cli.validate pack <pack_dir> --schema /path/to/pack_schema.yaml
    python -m sidequest.cli.validate pack <pack_dir> --verbose
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
import yaml
from pydantic import BaseModel, ValidationError

# Leaf model modules imported directly (NOT genre.loader) to avoid the
# session_handler/websocket_session_handler import cycle that the loader graph
# transitively pulls in (story 64-6). projection.rules and dungeon.themes are
# imported lazily inside their validators, mirroring loader.py's own lazy import.
from sidequest.genre.models.archetype_constraints import ArchetypeConstraints
from sidequest.genre.models.character import NpcArchetype
from sidequest.genre.models.pack import PortraitManifestEntry
from sidequest.genre.models.tropes import TropeDefinition

# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------


def load_pack_schema(schema_path: Path) -> dict[str, Any]:
    """Load and return the pack_schema.yaml as a dict.

    Raises ``FileNotFoundError`` if the path does not exist, and
    ``yaml.YAMLError`` if the file is not valid YAML. Neither is caught here
    — callers that need graceful handling should wrap the call.
    """
    raw = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"pack_schema.yaml at {schema_path} is not a mapping")
    return raw


# ---------------------------------------------------------------------------
# Low-level checkers
# ---------------------------------------------------------------------------


def _check_required_files(directory: Path, required: list[str], label: str) -> list[str]:
    """Return error strings for each required file missing from ``directory``."""
    errors: list[str] = []
    for fname in required:
        if not (directory / fname).is_file():
            errors.append(f"{label}: missing required file '{fname}'")
    return errors


def _check_required_dirs(directory: Path, required: list[str], label: str) -> list[str]:
    """Return error strings for each required directory missing from ``directory``."""
    errors: list[str] = []
    for dname in required:
        if not (directory / dname).is_dir():
            errors.append(f"{label}: missing required directory '{dname}'")
    return errors


def _resolve_extension_paths(
    extensions_declared: list[str],
    extensions_schema: dict[str, Any],
) -> tuple[set[str], set[str]]:
    """Given declared extension names and the schema's extensions dict, return
    ``(expected_files, expected_dirs)`` — the union of all files/dirs that the
    declared extensions require.

    Silently skips extension names that are not present in the schema (those
    are caught separately by ``_check_extensions``).
    """
    expected_files: set[str] = set()
    expected_dirs: set[str] = set()
    for ext_name in extensions_declared:
        ext_spec = extensions_schema.get(ext_name)
        if not isinstance(ext_spec, dict):
            continue
        for f in ext_spec.get("files", []):
            expected_files.add(f)
        for d in ext_spec.get("dirs", []):
            expected_dirs.add(d)
    return expected_files, expected_dirs


def _check_extensions(
    directory: Path,
    extensions_declared: list[str],
    extensions_schema: dict[str, Any],
    label: str,
) -> list[str]:
    """Check that every declared extension's required files/dirs are present.

    Returns error strings for each missing file/dir. Also errors if an
    extension name is declared but not present in the schema.
    """
    errors: list[str] = []
    for ext_name in extensions_declared:
        ext_spec = extensions_schema.get(ext_name)
        if not isinstance(ext_spec, dict):
            errors.append(f"{label}: declared extension '{ext_name}' is not defined in schema")
            continue
        for fname in ext_spec.get("files", []):
            if not (directory / fname).is_file():
                errors.append(f"{label}: extension '{ext_name}' requires missing file '{fname}'")
        for dname in ext_spec.get("dirs", []):
            if not (directory / dname).is_dir():
                errors.append(
                    f"{label}: extension '{ext_name}' requires missing directory '{dname}'"
                )
    return errors


def _check_orphans(
    directory: Path,
    required_files: list[str],
    required_dirs: list[str],
    extension_files: set[str],
    extension_dirs: set[str],
    genre_required_files: list[str],
    genre_extension_files: set[str],
    label: str,
) -> list[str]:
    """Find files/dirs in ``directory`` not accounted for by any schema entry.

    Returns warning strings for each orphan.

    Rules:
    - Dot-files (names starting with ``.``) are skipped entirely.
    - At world level, files that are required at genre level are valid
      overrides — not orphans.
    - ``known_dirs`` is derived from the first path component of required_dirs
      (e.g., ``audio/music`` → ``audio``).
    - The ``worlds`` directory at genre level is always known.
    """
    known_files: set[str] = (
        set(required_files) | extension_files | set(genre_required_files) | genre_extension_files
    )
    # Extract top-level dir names from required_dirs paths
    known_top_dirs: set[str] = set()
    for d in required_dirs + list(extension_dirs):
        top = Path(d).parts[0]
        known_top_dirs.add(top)
    known_top_dirs.add("worlds")

    warnings: list[str] = []
    for item in sorted(directory.iterdir()):
        name = item.name
        if name.startswith("."):
            continue
        if item.is_file() and name not in known_files:
            warnings.append(f"{label}: orphan file '{name}' (not in schema)")
        elif item.is_dir() and name not in known_top_dirs:
            warnings.append(f"{label}: orphan directory '{name}' (not in schema)")
    return warnings


# ---------------------------------------------------------------------------
# Content validation — parse present, schema-known files through their models
#
# Structural checks only prove a file EXISTS. These checks prove it PARSES:
# each present file with a known schema is run through its pydantic model (or
# loader), and parse/validation failures are reported as ERRORs carrying the
# filename and the underlying pydantic/YAML message. Files with no registered
# schema are not touched (no regression of the orphan-warning behavior).
#
# Top-level shape matches the loader: tropes.yaml / archetypes.yaml are YAML
# lists of dicts (loader._load_single_world l.779-791); a non-list / None
# document is skipped rather than flagged, so empty optional files stay valid.
# ---------------------------------------------------------------------------


def _read_yaml(path: Path, label: str) -> tuple[Any, str | None]:
    """Read+parse a YAML file. Returns ``(data, error)`` — exactly one is set
    meaningfully. A parse failure yields ``(None, "<file>: ...")``."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), None
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        return None, f"{label}: {path.name} is not valid YAML: {exc}"


def _validate_list_of_model(path: Path, model: type[BaseModel], label: str) -> list[str]:
    """Validate a file holding a YAML list of model dicts. Skips absent files
    and non-list documents (parity with the loader); reports each bad entry."""
    if not path.is_file():
        return []
    data, read_err = _read_yaml(path, label)
    if read_err is not None:
        return [read_err]
    if not isinstance(data, list):
        return []
    errors: list[str] = []
    for idx, entry in enumerate(data):
        try:
            model.model_validate(entry)
        except ValidationError as exc:
            errors.append(
                f"{label}: {path.name} entry [{idx}] failed {model.__name__} validation: {exc}"
            )
    return errors


def _validate_single_model(path: Path, model: type[BaseModel], label: str) -> list[str]:
    """Validate a file holding a single model mapping. Skips absent/empty files."""
    if not path.is_file():
        return []
    data, read_err = _read_yaml(path, label)
    if read_err is not None:
        return [read_err]
    if data is None:
        return []
    try:
        model.model_validate(data)
    except ValidationError as exc:
        return [f"{label}: {path.name} failed {model.__name__} validation: {exc}"]
    return []


def _collect_poi_slugs(history_path: Path) -> set[str]:
    """Return the set of ``slug`` values from all ``points_of_interest`` entries
    across all chapters in a ``history.yaml``.

    Uses ``_iter_history_chapters`` to accept both the top-level ``chapters:``
    shape (world histories) and the ``history_structure.chapters:`` shape
    (genre-tier templates). Returns an empty set when the file is absent,
    unparseable, or has no POI slugs.
    """
    if not history_path.is_file():
        return set()
    # Deliberate best-effort read: a malformed history.yaml yields set() here —
    # history.yaml correctness is validated independently by the world validator
    # (_validate_history_trope_refs), so slug collection can only false-negative.
    data, _read_err = _read_yaml(history_path, "")
    if data is None:
        return set()
    chapters = _iter_history_chapters(data)
    slugs: set[str] = set()
    for chapter in chapters:
        pois = chapter.get("points_of_interest")
        if not isinstance(pois, list):
            continue
        for poi in pois:
            if isinstance(poi, dict) and poi.get("slug"):
                slugs.add(str(poi["slug"]))
    return slugs


def _validate_portrait_manifest(
    path: Path,
    label: str,
    known_poi_slugs: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Validate portrait_manifest.yaml in both supported shapes:
    ``{characters: [...]}`` and a bare list (loader._load_portrait_manifest
    l.638-645). Skips absent/empty files and unrecognized top-level shapes.

    Returns ``(errors, warnings)``.

    - Pydantic ``PortraitManifestEntry`` failures → **errors**
    - ``type=player_picker`` missing required fields (``id``, ``culture``,
      ``archetype``, ``sex``) → **warnings** (content-gap signal; pack still loads)
    - ``backdrop_poi`` slug not in ``known_poi_slugs`` → **warnings** when
      ``known_poi_slugs`` is provided

    NPC entries (any other type) are completely unaffected by the picker checks.
    """
    if not path.is_file():
        return [], []
    data, read_err = _read_yaml(path, label)
    if read_err is not None:
        return [read_err], []
    if isinstance(data, dict) and "characters" in data:
        entries = data["characters"]
    elif isinstance(data, list):
        entries = data
    else:
        return [], []
    if not isinstance(entries, list):
        return [], []
    errors: list[str] = []
    warnings: list[str] = []
    for idx, entry in enumerate(entries):
        try:
            parsed = PortraitManifestEntry.model_validate(entry)
        except ValidationError as exc:
            errors.append(
                f"{label}: {path.name} entry [{idx}] failed PortraitManifestEntry validation: {exc}"
            )
            continue
        if parsed.character_type == "player_picker":
            missing = [
                field
                for field in ("id", "culture", "archetype", "sex")
                if not getattr(parsed, field)
            ]
            if missing:
                warnings.append(
                    f"{label}: {path.name} entry [{idx}] player_picker is missing required"
                    f" fields: {', '.join(missing)}"
                )
            if (
                parsed.backdrop_poi
                and known_poi_slugs is not None
                and parsed.backdrop_poi not in known_poi_slugs
            ):
                warnings.append(
                    f"{label}: {path.name} entry [{idx}] player_picker backdrop_poi"
                    f" '{parsed.backdrop_poi}' does not match any known POI slug"
                )
    return errors, warnings


def _validate_projection(path: Path, label: str) -> list[str]:
    """Validate projection.yaml through the projection loader + validator.

    Imports are lazy (mirroring loader.py l.1145-1146 — the loader also defers
    the projection import) and live inside the try so an import failure is
    reported as an error tied to the file rather than crashing the whole run.

    Story 96-1: also runs ``validate_visibility_coverage`` — a pack that ships
    projection rules must route NARRATION + SECRET_NOTE through visibility_tag
    (the ADR-104/-105 structural-hiding pair). Content-gate severity only: the
    loader does NOT enforce it, so minimal fixture packs still load. Absent
    projection.yaml stays valid (early return above).
    """
    if not path.is_file():
        return []
    try:
        from sidequest.game.projection.rules import load_rules_from_yaml_path
        from sidequest.game.projection.validator import (
            validate_projection_rules,
            validate_visibility_coverage,
        )

        rules = load_rules_from_yaml_path(path)
        validate_projection_rules(rules)
    except Exception as exc:  # noqa: BLE001 — surface any import/loader/validator failure
        return [f"{label}: {path.name} failed projection validation: {exc}"]
    return [f"{label}: {path.name}: {finding}" for finding in validate_visibility_coverage(rules)]


def _validate_theme_palette(pack_dir: Path, label: str) -> list[str]:
    """Validate WORLD-tier themes/*.yaml through the strict palette loader.

    ADR-140 (story 113-1): the dungeon theme palette is world-tier content,
    living at ``worlds/<world>/themes/`` — not at the genre-pack root. Scan
    each world for a themes/ dir and validate it through the loader. A world
    with no themes/ dir is not an error (the palette is dungeon-specific)."""
    worlds_dir = pack_dir / "worlds"
    if not worlds_dir.is_dir():
        return []
    from sidequest.dungeon.themes import ThemePaletteMissingError, load_theme_palette

    errors: list[str] = []
    for world_dir in sorted(worlds_dir.iterdir()):
        if not (world_dir / "themes").is_dir():
            continue
        try:
            load_theme_palette(world_dir)
        except ThemePaletteMissingError:
            continue
        except Exception as exc:  # noqa: BLE001 — palette loader fails loud with filename
            errors.append(
                f"{label}: worlds/{world_dir.name}/themes/ failed palette validation: {exc}"
            )
    return errors


# ---------------------------------------------------------------------------
# Cross-reference content lint (story 64-5)
#
# The per-file parse layer (story 64-4) proves each file PARSES through its
# leaf model. These checks prove CROSS-references between files resolve —
# references the per-file pydantic models cannot see on their own:
#
#   AC1  trope IDs referenced in history.yaml chapters AND legends'
#        related_tropes must exist in the resolved trope set
#        (genre tropes.yaml ids ∪ world tropes.yaml ids).
#   AC2  archetype typical_classes / typical_races must be in rules.yaml's
#        allowed_classes / allowed_races.
#   AC3  archetype_constraints valid_pairings jungian/role ids must be from the
#        canonical sets in the repo-global archetypes_base.yaml, and
#        genre_flavor must cover EXACTLY those jungian + rpg_role ids.
#
# Each violation is reported as an ERROR naming the offending id + file. The
# theme-palette adjacency closure (AC4) is already enforced by
# _validate_theme_palette → load_theme_palette and is not duplicated here.
# ---------------------------------------------------------------------------


def _find_archetypes_base(pack_dir: Path) -> Path:
    """Locate the repo-global ``archetypes_base.yaml`` by walking up from
    ``pack_dir`` (mirrors ``_find_default_schema``'s discovery of
    ``pack_schema.yaml``).

    Raises ``FileNotFoundError`` if not found — no silent fallback.
    """
    search = pack_dir.resolve()
    for _ in range(7):
        candidate = search / "archetypes_base.yaml"
        if candidate.is_file():
            return candidate
        sibling = search / "sidequest-content" / "archetypes_base.yaml"
        if sibling.is_file():
            return sibling
        search = search.parent
    raise FileNotFoundError(
        f"Could not locate archetypes_base.yaml from '{pack_dir}' "
        f"(required to validate archetype_constraints.yaml)."
    )


def _collect_trope_ids(path: Path) -> set[str]:
    """Collect declared trope ids from a tropes.yaml (a YAML list of dicts).

    Only entries carrying an explicit ``id`` contribute — matching the
    "resolved trope set = union of genre + world tropes.yaml ids" contract.
    Absent / non-list files contribute nothing (parity with the loader)."""
    ids: set[str] = set()
    if not path.is_file():
        return ids
    data, _read_err = _read_yaml(path, "")
    if not isinstance(data, list):
        return ids
    for entry in data:
        if isinstance(entry, dict) and entry.get("id"):
            ids.add(str(entry["id"]))
    return ids


def _iter_history_chapters(history_data: Any) -> list[dict[str, Any]]:
    """Return the chapter dicts from a parsed history.yaml, accepting both the
    top-level ``chapters:`` shape (world histories) and the
    ``history_structure.chapters:`` shape (genre-tier history templates)."""
    chapters: list[dict[str, Any]] = []
    if not isinstance(history_data, dict):
        return chapters
    for source in (history_data.get("chapters"), None):
        if isinstance(source, list):
            chapters.extend(c for c in source if isinstance(c, dict))
    hs = history_data.get("history_structure")
    if isinstance(hs, dict) and isinstance(hs.get("chapters"), list):
        chapters.extend(c for c in hs["chapters"] if isinstance(c, dict))
    return chapters


def _validate_history_trope_refs(
    history_path: Path, resolved_ids: set[str], label: str
) -> list[str]:
    """AC1 — every trope id REFERENCED in a history chapter must resolve.

    A chapter ``tropes`` entry that carries a ``name`` is an inline trope
    DEFINITION (seeded campaign state), not a reference — it contributes its own
    id to the resolved set and is not itself validated. Bare instance refs (a
    string, or a dict with ``id`` and no ``name``) must resolve against the
    union of the resolved set + inline-defined ids."""
    if not history_path.is_file():
        return []
    data, read_err = _read_yaml(history_path, label)
    if read_err is not None:
        # No pydantic model is wired to history.yaml, so the cross-ref pass is
        # the only line of defence — surface the parse failure loudly (it names
        # the file) instead of swallowing it (No Silent Fallbacks).
        return [read_err]
    chapters = _iter_history_chapters(data)

    # Pass 1: collect inline-defined ids across all chapters.
    inline_ids: set[str] = set()
    bare_refs: list[str] = []
    for chapter in chapters:
        entries = chapter.get("tropes")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                bare_refs.append(entry)
            elif isinstance(entry, dict):
                if entry.get("name") is not None:
                    if entry.get("id"):
                        inline_ids.add(str(entry["id"]))
                elif entry.get("id"):
                    bare_refs.append(str(entry["id"]))

    # Pass 2: validate bare refs against the resolved set + inline defs.
    known = resolved_ids | inline_ids
    errors: list[str] = []
    for tid in bare_refs:
        if tid not in known:
            errors.append(
                f"{label}: history.yaml references unknown trope id '{tid}' "
                f"(not in resolved trope set)"
            )
    return errors


def _validate_legend_trope_refs(legends_dir: Path, resolved_ids: set[str], label: str) -> list[str]:
    """AC1 — every id in a legend's ``related_tropes`` must resolve against the
    resolved trope set. Absent legends/ dir contributes nothing."""
    if not legends_dir.is_dir():
        return []
    errors: list[str] = []
    for legend_path in sorted(legends_dir.glob("*.yaml")):
        data, read_err = _read_yaml(legend_path, label)
        if read_err is not None:
            # No pydantic model is wired to legend files, so the cross-ref pass
            # is the only line of defence — surface the parse failure loudly (it
            # names the file) instead of skipping past it (No Silent Fallbacks).
            errors.append(read_err)
            continue
        if not isinstance(data, dict):
            continue
        related = data.get("related_tropes")
        if not isinstance(related, list):
            continue
        for tid in related:
            if str(tid) not in resolved_ids:
                errors.append(
                    f"{label}: legend '{legend_path.name}' references unknown trope id "
                    f"'{tid}' (not in resolved trope set)"
                )
    return errors


def _read_allowed_sets(rules_path: Path) -> tuple[set[str], set[str]] | None:
    """Read ``allowed_classes`` / ``allowed_races`` from rules.yaml as plain
    string sets. Returns ``None`` when rules.yaml is absent or unparseable.

    Read raw (``yaml.safe_load``) rather than through ``RulesConfig`` /
    ``_load_rules_config``: the latter resolves ``_from:`` confrontation
    pointers, which would require sibling include files that synthetic fixtures
    do not ship. ``allowed_classes`` / ``allowed_races`` are plain lists, never
    ``_from:`` indirected, so a raw read is both correct and self-contained."""
    if not rules_path.is_file():
        return None
    data, _read_err = _read_yaml(rules_path, "")
    if not isinstance(data, dict):
        return None
    classes = {str(c) for c in (data.get("allowed_classes") or [])}
    races = {str(r) for r in (data.get("allowed_races") or [])}
    return classes, races


def _validate_archetype_class_race(
    archetypes_path: Path,
    allowed_classes: set[str],
    allowed_races: set[str],
    label: str,
) -> list[str]:
    """AC2 — archetype typical_classes / typical_races must be in the pack's
    allowed_classes / allowed_races. A dimension with an empty allowed set is
    not validated (nothing to resolve against)."""
    if not archetypes_path.is_file():
        return []
    data, read_err = _read_yaml(archetypes_path, label)
    if read_err is not None:
        return []
    if not isinstance(data, list):
        return []
    errors: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "<unnamed>")
        if allowed_classes:
            for cls in entry.get("typical_classes", []) or []:
                if str(cls) not in allowed_classes:
                    errors.append(
                        f"{label}: archetypes.yaml archetype '{name}' references class "
                        f"'{cls}' not in rules.yaml allowed_classes"
                    )
        if allowed_races:
            for race in entry.get("typical_races", []) or []:
                if str(race) not in allowed_races:
                    errors.append(
                        f"{label}: archetypes.yaml archetype '{name}' references race "
                        f"'{race}' not in rules.yaml allowed_races"
                    )
    return errors


def _validate_archetype_constraints_crossref(pack_dir: Path, label: str) -> list[str]:
    """AC3 — validate archetype_constraints.yaml cross-references against the
    canonical archetype axes in archetypes_base.yaml:

    * every valid_pairings ``[jungian, role]`` id must be canonical;
    * genre_flavor.jungian keys must cover EXACTLY the canonical jungian set;
    * genre_flavor.rpg_roles keys must cover EXACTLY the canonical role set.

    ``npc_roles_available`` is intentionally NOT checked here — it is a separate
    field against the separate npc_roles set. No-op when the file is absent."""
    constraints_path = pack_dir / "archetype_constraints.yaml"
    if not constraints_path.is_file():
        return []
    data, read_err = _read_yaml(constraints_path, label)
    if read_err is not None:
        return []
    if not isinstance(data, dict):
        return []

    # Canonical axes — fail loudly (as a visible ERROR) if archetypes_base.yaml
    # is unreachable rather than silently skipping the check.
    try:
        base_path = _find_archetypes_base(pack_dir)
        from sidequest.genre.models.archetype_axes import BaseArchetypes

        base_raw = yaml.safe_load(base_path.read_text(encoding="utf-8"))
        base = BaseArchetypes.model_validate(base_raw)
    except Exception as exc:  # noqa: BLE001 — surface discovery/parse failure loudly
        return [
            f"{label}: cannot resolve canonical archetype axes for "
            f"archetype_constraints.yaml: {exc}"
        ]

    canonical_jungian = {j.id for j in base.jungian}
    canonical_roles = {r.id for r in base.rpg_roles}
    errors: list[str] = []

    # valid_pairings — each [jungian, role] must be canonical.
    pairings = data.get("valid_pairings")
    if isinstance(pairings, dict):
        for weight in ("common", "uncommon", "rare", "forbidden"):
            for pair in pairings.get(weight, []) or []:
                if not isinstance(pair, list) or len(pair) != 2:
                    continue
                jungian, role = str(pair[0]), str(pair[1])
                if jungian not in canonical_jungian:
                    errors.append(
                        f"{label}: archetype_constraints.yaml valid_pairings references "
                        f"non-canonical jungian id '{jungian}'"
                    )
                if role not in canonical_roles:
                    errors.append(
                        f"{label}: archetype_constraints.yaml valid_pairings references "
                        f"non-canonical rpg_role id '{role}'"
                    )

    # genre_flavor — must cover EXACTLY the canonical jungian + role sets.
    genre_flavor = data.get("genre_flavor")
    if isinstance(genre_flavor, dict):
        flavor_jungian = set((genre_flavor.get("jungian") or {}).keys())
        flavor_roles = set((genre_flavor.get("rpg_roles") or {}).keys())
        for missing in sorted(canonical_jungian - flavor_jungian):
            errors.append(
                f"{label}: archetype_constraints.yaml genre_flavor.jungian is missing "
                f"canonical jungian id '{missing}'"
            )
        for extra in sorted(flavor_jungian - canonical_jungian):
            errors.append(
                f"{label}: archetype_constraints.yaml genre_flavor.jungian has "
                f"non-canonical jungian id '{extra}'"
            )
        for missing in sorted(canonical_roles - flavor_roles):
            errors.append(
                f"{label}: archetype_constraints.yaml genre_flavor.rpg_roles is missing "
                f"canonical rpg_role id '{missing}'"
            )
        for extra in sorted(flavor_roles - canonical_roles):
            errors.append(
                f"{label}: archetype_constraints.yaml genre_flavor.rpg_roles has "
                f"non-canonical rpg_role id '{extra}'"
            )

    return errors


# ---------------------------------------------------------------------------
# ADR-143 — chargen cross-reference lint (Task 13)
#
# Four checks mirror the runtime resolution rules in chargen_defs_resolve.py:
#
#   CG1  Every ``background:`` id in char_creation.yaml's mechanical_effects
#        blocks must resolve against the effective background catalog for that
#        scope (world-tier backgrounds when the world defines them; otherwise
#        genre-tier backgrounds).
#   CG2  Every ``focus_id:`` in char_creation.yaml's mechanical_effects blocks
#        must resolve against the effective focus catalog (same resolution).
#   CG3  Every skill NAME used as a key in ``skill_grants`` (in
#        char_creation.yaml), ``Background.free_skill``, ``Background.quick_skills``
#        (in backgrounds.yaml), and ``FocusLevel.skills`` keys (in foci.yaml)
#        must appear in the genre-tier ``skills.yaml`` catalog.  Skills are
#        always genre-tier — there is no world-tier skills catalog.
#
# A missing background/focus/skill is a LOUD validation error (No Silent
# Fallbacks).  The check is a no-op for packs / worlds that author no
# skills.yaml / backgrounds.yaml / foci.yaml / char_creation.yaml.
#
# Resolution mirrors chargen_defs_resolve.resolve_backgrounds/resolve_foci:
# world-tier dicts REPLACE the genre-tier dict (no merge).  A world that
# defines its own backgrounds.yaml uses ONLY those backgrounds for scope
# isolation; a world without one falls through to the genre-tier dict.
# ---------------------------------------------------------------------------


def _collect_skill_catalog(skills_path: Path) -> set[str]:
    """Return the set of skill names from a genre-tier ``skills.yaml``.

    Returns an empty set when the file is absent (no catalog → no CG3 checks).
    A present-but-malformed file also returns empty set (the YAML structural
    check will catch the malformation separately; we don't double-error here).
    """
    if not skills_path.is_file():
        return set()
    data, _read_err = _read_yaml(skills_path, "")
    if not isinstance(data, list):
        return set()
    return {str(s) for s in data if isinstance(s, str)}


def _collect_background_ids(backgrounds_path: Path) -> set[str]:
    """Return the set of background ``id`` values from a ``backgrounds.yaml``.

    Returns an empty set when the file is absent or unparseable.
    """
    if not backgrounds_path.is_file():
        return set()
    data, _read_err = _read_yaml(backgrounds_path, "")
    if not isinstance(data, list):
        return set()
    ids: set[str] = set()
    for entry in data:
        if isinstance(entry, dict) and entry.get("id"):
            ids.add(str(entry["id"]))
    return ids


def _collect_focus_ids(foci_path: Path) -> set[str]:
    """Return the set of focus ``id`` values from a ``foci.yaml``.

    Returns an empty set when the file is absent or unparseable.
    """
    if not foci_path.is_file():
        return set()
    data, _read_err = _read_yaml(foci_path, "")
    if not isinstance(data, list):
        return set()
    ids: set[str] = set()
    for entry in data:
        if isinstance(entry, dict) and entry.get("id"):
            ids.add(str(entry["id"]))
    return ids


def _iter_char_creation_mechanical_effects(data: Any) -> list[dict[str, Any]]:
    """Return all ``mechanical_effects`` dicts from a parsed char_creation.yaml.

    Walks scene-level ``mechanical_effects`` and all ``choices[*].mechanical_effects``
    in every scene, returning a flat list of the raw effect dicts.  Accepts ``None``
    or non-list data gracefully (returns ``[]``).
    """
    effects: list[dict[str, Any]] = []
    if not isinstance(data, list):
        return effects
    for scene in data:
        if not isinstance(scene, dict):
            continue
        # Scene-level mechanical_effects
        me = scene.get("mechanical_effects")
        if isinstance(me, dict):
            effects.append(me)
        # Choice-level mechanical_effects
        for choice in scene.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            cme = choice.get("mechanical_effects")
            if isinstance(cme, dict):
                effects.append(cme)
    return effects


def _validate_chargen_crossref(
    char_creation_path: Path,
    backgrounds_path: Path,
    foci_path: Path,
    skill_catalog: set[str],
    label: str,
    scope_label: str = "",
) -> list[str]:
    """CG1 + CG2 + CG3 for a single char_creation.yaml.

    ``backgrounds_path`` and ``foci_path`` are the EFFECTIVE paths after world-first
    resolution (caller resolves which tier to use).  ``skill_catalog`` is always
    the genre-tier set (there is no world-tier skills catalog).
    ``scope_label`` is appended to error messages to distinguish genre-tier
    vs world-tier sources (e.g. "world 'evropi'").
    """
    if not char_creation_path.is_file():
        return []

    data, read_err = _read_yaml(char_creation_path, label)
    if read_err is not None:
        return [read_err]

    known_background_ids = _collect_background_ids(backgrounds_path)
    known_focus_ids = _collect_focus_ids(foci_path)
    # Only check resolution when this pack actually ships a catalog — an absent
    # backgrounds/foci file means this pack authors none (no-op for non-WWN packs).
    check_backgrounds = backgrounds_path.is_file()
    check_foci = foci_path.is_file()
    check_skills = bool(skill_catalog)

    errors: list[str] = []
    scope = f" ({scope_label})" if scope_label else ""
    effects = _iter_char_creation_mechanical_effects(data)

    for effect in effects:
        # CG1 — background id resolution
        bg_ref = effect.get("background")
        if bg_ref and check_backgrounds and str(bg_ref) not in known_background_ids:
            errors.append(
                f"{label}: char_creation.yaml references unknown background id "
                f"'{bg_ref}'{scope} (not in resolved backgrounds catalog)"
            )
        # CG2 — focus_id resolution
        focus_ref = effect.get("focus_id")
        if focus_ref and check_foci and str(focus_ref) not in known_focus_ids:
            errors.append(
                f"{label}: char_creation.yaml references unknown focus_id "
                f"'{focus_ref}'{scope} (not in resolved foci catalog)"
            )
        # CG3 — skill_grants keys against skills catalog
        if check_skills:
            for skill_name in (effect.get("skill_grants") or {}):
                if str(skill_name) not in skill_catalog:
                    errors.append(
                        f"{label}: char_creation.yaml skill_grants references unknown "
                        f"skill '{skill_name}'{scope} (not in skills.yaml catalog)"
                    )

    return errors


def _validate_backgrounds_skill_refs(
    backgrounds_path: Path,
    skill_catalog: set[str],
    label: str,
) -> list[str]:
    """CG3 — validate skill names in ``backgrounds.yaml`` against the catalog.

    Checks ``free_skill`` (a single skill string) and ``quick_skills`` (a list
    of skill strings).  No-op when the file is absent or the catalog is empty.
    """
    if not backgrounds_path.is_file() or not skill_catalog:
        return []
    data, read_err = _read_yaml(backgrounds_path, label)
    if read_err is not None:
        return [read_err]
    if not isinstance(data, list):
        return []
    errors: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        bg_id = entry.get("id", "<unnamed>")
        # free_skill — a single string or None
        free = entry.get("free_skill")
        if isinstance(free, str) and free and free not in skill_catalog:
            errors.append(
                f"{label}: backgrounds.yaml background '{bg_id}' free_skill "
                f"references unknown skill '{free}' (not in skills.yaml catalog)"
            )
        # quick_skills — a list of strings
        for qs in entry.get("quick_skills") or []:
            if isinstance(qs, str) and qs and qs not in skill_catalog:
                errors.append(
                    f"{label}: backgrounds.yaml background '{bg_id}' quick_skills "
                    f"references unknown skill '{qs}' (not in skills.yaml catalog)"
                )
    return errors


def _validate_foci_skill_refs(
    foci_path: Path,
    skill_catalog: set[str],
    label: str,
) -> list[str]:
    """CG3 — validate skill names in ``foci.yaml`` levels against the catalog.

    Checks each ``FocusLevel.skills`` dict key.  No-op when the file is absent
    or the catalog is empty.
    """
    if not foci_path.is_file() or not skill_catalog:
        return []
    data, read_err = _read_yaml(foci_path, label)
    if read_err is not None:
        return [read_err]
    if not isinstance(data, list):
        return []
    errors: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        focus_id = entry.get("id", "<unnamed>")
        for lvl_idx, level in enumerate(entry.get("levels") or []):
            if not isinstance(level, dict):
                continue
            for skill_name in (level.get("skills") or {}):
                if str(skill_name) not in skill_catalog:
                    errors.append(
                        f"{label}: foci.yaml focus '{focus_id}' level[{lvl_idx}] "
                        f"skills references unknown skill '{skill_name}' "
                        f"(not in skills.yaml catalog)"
                    )
    return errors


# ---------------------------------------------------------------------------
# Epic 74 — lore is world-only and must be non-empty (story 74-3)
# ---------------------------------------------------------------------------


def _validate_world_lore_seedable(world_dir: Path, label: str) -> list[str]:
    """A world's ``lore.yaml`` must seed at least one LoreStore fragment.

    Epic 74: lore is world-only and authoritative; genre lore is no longer
    seeded. The narrator's ``seed_lore_from_world`` only reads
    ``history`` / ``geography`` / ``cosmology`` (non-empty strings) and
    ``factions`` (a non-empty list) — content under any OTHER key is silently
    ignored. A world whose lore.yaml carries only non-seedable keys would leave
    the narrator's LoreStore empty (No Silent Fallbacks). Absence of the file is
    handled by the structural required-files check, not here.

    Mirrors the load-time guard in ``genre.loader._world_lore_seedable_count`` —
    kept here as a raw-YAML check so the validator stays free of the loader
    import graph (story 64-6).
    """
    lore_path = world_dir / "lore.yaml"
    if not lore_path.is_file():
        return []
    data, read_err = _read_yaml(lore_path, label)
    if read_err is not None:
        return [read_err]
    if not isinstance(data, dict):
        return [f"{label}: lore.yaml is not a mapping"]
    has_text = any(
        isinstance(data.get(k), str) and data.get(k, "").strip()
        for k in ("history", "geography", "cosmology")
    )
    factions = data.get("factions")
    has_factions = isinstance(factions, list) and len(factions) > 0
    if not (has_text or has_factions):
        return [
            f"{label}: lore.yaml has no seedable lore — it must populate at least "
            "one of history / geography / cosmology (non-empty string) or factions "
            "(non-empty list). Content under other keys is NOT seeded into the "
            "narrator's LoreStore; an empty world LoreStore is forbidden (Epic 74)."
        ]
    return []


# ---------------------------------------------------------------------------
# World-level validation
# ---------------------------------------------------------------------------


def _validate_world(
    world_dir: Path,
    world_schema: dict[str, Any],
    genre_schema: dict[str, Any],
    genre_ext_files: set[str],
    genre_ext_dirs: set[str],
    genre_extensions_declared: list[str],
    genre_trope_ids: set[str],
    genre_allowed: tuple[set[str], set[str]] | None,
    genre_skill_catalog: set[str],
    genre_backgrounds_path: Path,
    genre_foci_path: Path,
) -> tuple[list[str], list[str]]:
    """Validate a single world directory.

    If ``world.yaml`` contains ``draft: true``, all structural problems are
    demoted to warnings instead of errors.

    ``genre_trope_ids`` and ``genre_allowed`` (the genre-tier resolved trope set
    and allowed class/race sets) are threaded down so the world-tier cross-ref
    lint can resolve references against the union of genre + world content.

    ``genre_skill_catalog``, ``genre_backgrounds_path``, and ``genre_foci_path``
    support ADR-143 chargen cross-reference checks (CG1/CG2/CG3).  The genre-tier
    skill catalog may be empty and the genre-tier background/foci paths may point
    at absent files — each downstream helper no-ops on an empty catalog / missing
    file, so non-WWN packs need no special-casing here.  The world-tier chargen
    is validated using world-first resolution: if the world defines its own
    backgrounds/foci, those replace the genre-tier defs; skills always come from
    the genre-tier catalog.

    Returns ``(errors, warnings)``.
    """
    label = f"world '{world_dir.name}'"

    # Load world.yaml for draft status and extensions. A parse failure is a
    # hard error reported loudly (No Silent Fallbacks) — not swallowed, and not
    # demoted by draft status (we cannot read the draft flag from broken YAML).
    world_yaml_path = world_dir / "world.yaml"
    world_data: dict[str, Any] = {}
    hard_errors: list[str] = []
    if world_yaml_path.is_file():
        try:
            world_data = yaml.safe_load(world_yaml_path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            hard_errors.append(f"{label}: world.yaml is not valid YAML: {exc}")
            world_data = {}
    is_draft = bool(world_data.get("draft", False))

    world_required_files: list[str] = world_schema.get("required_files", [])
    world_required_dirs: list[str] = world_schema.get("required_dirs", [])
    world_extensions_schema: dict[str, Any] = world_schema.get("extensions", {})
    world_extensions_declared: list[str] = world_data.get("extensions", [])

    # Explicit per-world completeness waiver. A live (non-draft) world that
    # legitimately loads but is intentionally incomplete — e.g. an imported
    # campaign skeleton whose remaining canon must NOT be fabricated — may
    # declare ``incomplete_files`` / ``incomplete_dirs`` in world.yaml to waive
    # those SPECIFIC required artifacts. This is the opposite of a silent
    # fallback: each waived path is named in the world's own metadata (loudly,
    # with an author rationale in a comment) and the validator still EMITS A
    # WARNING for every waived item so it shows in --verbose and never silently
    # disappears. Any required artifact NOT named here still hard-errors, and
    # the waiver only applies to required_files/required_dirs the loader itself
    # treats as optional — it cannot wave through a file the loader hard-requires
    # (those would fail at load time regardless). Worlds that ship every required
    # file are unaffected. Draft worlds ignore this (they already demote
    # everything to warnings).
    waived_files: set[str] = {str(f) for f in (world_data.get("incomplete_files") or [])}
    waived_dirs: set[str] = {str(d) for d in (world_data.get("incomplete_dirs") or [])}

    enforced_required_files = [f for f in world_required_files if f not in waived_files]
    enforced_required_dirs = [d for d in world_required_dirs if d not in waived_dirs]
    waiver_warnings: list[str] = [
        f"{label}: required file '{f}' WAIVED via world.yaml incomplete_files "
        f"(live-but-incomplete world; artifact intentionally unauthored)"
        for f in world_required_files
        if f in waived_files and not (world_dir / f).is_file()
    ] + [
        f"{label}: required directory '{d}' WAIVED via world.yaml incomplete_dirs "
        f"(live-but-incomplete world; artifact intentionally unauthored)"
        for d in world_required_dirs
        if d in waived_dirs and not (world_dir / d).is_dir()
    ]

    world_ext_files, world_ext_dirs = _resolve_extension_paths(
        world_extensions_declared, world_extensions_schema
    )

    structural_errors: list[str] = []
    structural_errors.extend(_check_required_files(world_dir, enforced_required_files, label))
    structural_errors.extend(_check_required_dirs(world_dir, enforced_required_dirs, label))
    structural_errors.extend(
        _check_extensions(world_dir, world_extensions_declared, world_extensions_schema, label)
    )

    # Genre-level files are valid overrides at world level — not orphans
    genre_required = genre_schema.get("required_files", [])
    genre_ext_all_files: set[str] = set()
    for ext_name in genre_extensions_declared:
        ext_spec = genre_schema.get("extensions", {}).get(ext_name, {})
        for f in ext_spec.get("files", []):
            genre_ext_all_files.add(f)

    orphan_warnings = _check_orphans(
        directory=world_dir,
        required_files=world_required_files,
        required_dirs=world_required_dirs,
        extension_files=world_ext_files,
        extension_dirs=world_ext_dirs,
        genre_required_files=genre_required,
        genre_extension_files=genre_ext_all_files,
        label=label,
    )

    # Content validation: parse present, schema-known world-tier files.
    content_errors: list[str] = []
    content_errors.extend(
        _validate_list_of_model(world_dir / "archetypes.yaml", NpcArchetype, label)
    )
    content_errors.extend(
        _validate_list_of_model(world_dir / "tropes.yaml", TropeDefinition, label)
    )
    poi_slugs = _collect_poi_slugs(world_dir / "history.yaml")
    manifest_errors, manifest_warnings = _validate_portrait_manifest(
        world_dir / "portrait_manifest.yaml",
        label,
        known_poi_slugs=poi_slugs,
    )
    content_errors.extend(manifest_errors)
    waiver_warnings.extend(manifest_warnings)

    # Epic 74 (story 74-3) — world lore must seed a non-empty LoreStore.
    content_errors.extend(_validate_world_lore_seedable(world_dir, label))

    # Cross-reference content lint (story 64-5) — world tier.
    resolved_trope_ids = genre_trope_ids | _collect_trope_ids(world_dir / "tropes.yaml")
    content_errors.extend(
        _validate_history_trope_refs(world_dir / "history.yaml", resolved_trope_ids, label)
    )
    content_errors.extend(
        _validate_legend_trope_refs(world_dir / "legends", resolved_trope_ids, label)
    )
    # World rules.yaml overrides genre allowed sets when present; otherwise the
    # genre-tier allowed sets govern this world's archetypes.
    world_allowed = _read_allowed_sets(world_dir / "rules.yaml") or genre_allowed
    if world_allowed is not None:
        content_errors.extend(
            _validate_archetype_class_race(
                world_dir / "archetypes.yaml", world_allowed[0], world_allowed[1], label
            )
        )

    # ADR-143 (Task 13) — chargen cross-reference lint at world tier.
    # CG1/CG2: background/focus ids in world-tier char_creation.yaml must
    # resolve against the effective catalog (world-tier if defined, else genre).
    # CG3: skill names in chargen files + backgrounds + foci must be in the
    # genre-tier skills catalog.
    # Resolution mirrors chargen_defs_resolve: world backgrounds/foci REPLACE
    # genre (no merge) — a world with its own backgrounds.yaml uses only those.
    # Each helper no-ops on an empty catalog / absent file, so non-WWN worlds
    # fall straight through without a guard here.
    world_char_creation_path = world_dir / "char_creation.yaml"
    world_backgrounds_path = world_dir / "backgrounds.yaml"
    world_foci_path = world_dir / "foci.yaml"

    # Resolve effective backgrounds + foci paths (world-first).
    eff_backgrounds_path = (
        world_backgrounds_path if world_backgrounds_path.is_file() else genre_backgrounds_path
    )
    eff_foci_path = world_foci_path if world_foci_path.is_file() else genre_foci_path

    # CG1 + CG2 + CG3(skill_grants) on world char_creation.yaml
    content_errors.extend(
        _validate_chargen_crossref(
            world_char_creation_path,
            eff_backgrounds_path,
            eff_foci_path,
            genre_skill_catalog,
            label,
            scope_label=f"world '{world_dir.name}'",
        )
    )

    # CG3 on world-tier backgrounds.yaml (free_skill + quick_skills)
    content_errors.extend(
        _validate_backgrounds_skill_refs(world_backgrounds_path, genre_skill_catalog, label)
    )

    # CG3 on world-tier foci.yaml (FocusLevel.skills keys)
    content_errors.extend(
        _validate_foci_skill_refs(world_foci_path, genre_skill_catalog, label)
    )

    if is_draft:
        # Demote structural + content problems to warnings for draft worlds.
        # A world.yaml parse failure is never demoted — it's an unconditional error.
        return (
            hard_errors,
            structural_errors + content_errors + orphan_warnings + waiver_warnings,
        )
    else:
        return (
            hard_errors + structural_errors + content_errors,
            orphan_warnings + waiver_warnings,
        )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def validate_pack_structure(pack_dir: Path, schema_path: Path) -> tuple[list[str], list[str]]:
    """Validate a genre pack directory against the schema.

    Returns ``(errors, warnings)``.

    Validates:
    1. Genre-level required files
    2. Genre-level required dirs
    3. Declared extensions' required files/dirs (from ``pack.yaml`` if present)
    4. Orphan files/dirs at genre level (as warnings)
    5. Each world directory under ``worlds/``
    """
    schema = load_pack_schema(schema_path)
    genre_schema: dict[str, Any] = schema.get("genre_pack", {})
    world_schema: dict[str, Any] = schema.get("world", {})

    genre_required_files: list[str] = genre_schema.get("required_files", [])
    genre_required_dirs: list[str] = genre_schema.get("required_dirs", [])
    genre_extensions_schema: dict[str, Any] = genre_schema.get("extensions", {})

    label = f"pack '{pack_dir.name}'"

    # Read declared extensions from pack.yaml
    extensions_declared: list[str] = []
    pack_yaml_path = pack_dir / "pack.yaml"
    if pack_yaml_path.is_file():
        try:
            raw = yaml.safe_load(pack_yaml_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                exts = raw.get("extensions", [])
                if isinstance(exts, list):
                    extensions_declared = [str(e) for e in exts]
        except (yaml.YAMLError, UnicodeDecodeError):
            pass

    all_errors: list[str] = []
    all_warnings: list[str] = []

    # Genre-level required files
    all_errors.extend(_check_required_files(pack_dir, genre_required_files, label))

    # Genre-level required dirs
    all_errors.extend(_check_required_dirs(pack_dir, genre_required_dirs, label))

    # Extension files/dirs
    all_errors.extend(
        _check_extensions(pack_dir, extensions_declared, genre_extensions_schema, label)
    )

    # Epic 74 (story 74-3): lore is world-only — a genre-tier lore.yaml is
    # forbidden (the genre tier is mechanics-only). Flag it loudly rather than
    # letting it fall through to a soft orphan warning.
    if (pack_dir / "lore.yaml").is_file():
        all_errors.append(
            f"{label}: genre-tier lore.yaml is forbidden (Epic 74: lore is "
            "world-only). Move its content into worlds/<world>/lore.yaml."
        )

    # Content validation: parse present, schema-known genre-tier files.
    all_errors.extend(_validate_list_of_model(pack_dir / "archetypes.yaml", NpcArchetype, label))
    all_errors.extend(_validate_list_of_model(pack_dir / "tropes.yaml", TropeDefinition, label))
    all_errors.extend(
        _validate_single_model(pack_dir / "archetype_constraints.yaml", ArchetypeConstraints, label)
    )
    all_errors.extend(_validate_projection(pack_dir / "projection.yaml", label))
    all_errors.extend(_validate_theme_palette(pack_dir, label))

    # Cross-reference content lint (story 64-5) — genre tier.
    genre_trope_ids = _collect_trope_ids(pack_dir / "tropes.yaml")
    genre_allowed = _read_allowed_sets(pack_dir / "rules.yaml")
    all_errors.extend(
        _validate_history_trope_refs(pack_dir / "history.yaml", genre_trope_ids, label)
    )
    all_errors.extend(_validate_legend_trope_refs(pack_dir / "legends", genre_trope_ids, label))
    if genre_allowed is not None:
        all_errors.extend(
            _validate_archetype_class_race(
                pack_dir / "archetypes.yaml", genre_allowed[0], genre_allowed[1], label
            )
        )
    all_errors.extend(_validate_archetype_constraints_crossref(pack_dir, label))

    # ADR-143 (Task 13) — chargen cross-reference lint at genre tier.
    # CG1 + CG2 + CG3(skill_grants) on genre-tier char_creation.yaml (when present).
    # CG3 on genre-tier backgrounds.yaml (free_skill + quick_skills).
    # CG3 on genre-tier foci.yaml (FocusLevel.skills keys).
    genre_backgrounds_path = pack_dir / "backgrounds.yaml"
    genre_foci_path = pack_dir / "foci.yaml"
    genre_skill_catalog = _collect_skill_catalog(pack_dir / "skills.yaml")
    all_errors.extend(
        _validate_chargen_crossref(
            pack_dir / "char_creation.yaml",
            genre_backgrounds_path,
            genre_foci_path,
            genre_skill_catalog,
            label,
            scope_label="genre tier",
        )
    )
    all_errors.extend(
        _validate_backgrounds_skill_refs(genre_backgrounds_path, genre_skill_catalog, label)
    )
    all_errors.extend(
        _validate_foci_skill_refs(genre_foci_path, genre_skill_catalog, label)
    )

    # Resolve extension paths for orphan check
    genre_ext_files, genre_ext_dirs = _resolve_extension_paths(
        extensions_declared, genre_extensions_schema
    )

    # Orphan check at genre level
    all_warnings.extend(
        _check_orphans(
            directory=pack_dir,
            required_files=genre_required_files,
            required_dirs=genre_required_dirs,
            extension_files=genre_ext_files,
            extension_dirs=genre_ext_dirs,
            genre_required_files=[],
            genre_extension_files=set(),
            label=label,
        )
    )

    # Validate worlds
    worlds_dir = pack_dir / "worlds"
    if worlds_dir.is_dir():
        for world_dir in sorted(worlds_dir.iterdir()):
            if not world_dir.is_dir():
                continue
            if world_dir.name.startswith("."):
                continue
            w_errors, w_warnings = _validate_world(
                world_dir=world_dir,
                world_schema=world_schema,
                genre_schema=genre_schema,
                genre_ext_files=genre_ext_files,
                genre_ext_dirs=genre_ext_dirs,
                genre_extensions_declared=extensions_declared,
                genre_trope_ids=genre_trope_ids,
                genre_allowed=genre_allowed,
                genre_skill_catalog=genre_skill_catalog,
                genre_backgrounds_path=genre_backgrounds_path,
                genre_foci_path=genre_foci_path,
            )
            all_errors.extend(w_errors)
            all_warnings.extend(w_warnings)

    return all_errors, all_warnings


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


def _find_default_schema(pack_dir: Path) -> Path:
    """Attempt to find pack_schema.yaml relative to pack_dir.

    Walks up the directory tree looking for sidequest-content/pack_schema.yaml,
    or tries a sibling directory named sidequest-content.

    Raises ``FileNotFoundError`` if no schema is found — no silent fallbacks.
    """
    # Try up to 5 levels up, looking for pack_schema.yaml next to or inside
    # a sidequest-content directory
    search = pack_dir.resolve()
    for _ in range(7):
        candidate = search / "pack_schema.yaml"
        if candidate.is_file():
            return candidate
        # Look for sidequest-content sibling
        sibling = search / "sidequest-content" / "pack_schema.yaml"
        if sibling.is_file():
            return sibling
        search = search.parent

    raise FileNotFoundError(
        f"Could not locate pack_schema.yaml from '{pack_dir}'. Pass --schema explicitly."
    )


@click.command()
@click.argument(
    "pack_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--schema",
    "schema_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to pack_schema.yaml (auto-detected if not given).",
)
@click.option("--verbose", is_flag=True, help="Show all issues even for passing packs.")
def main(pack_dir: Path, schema_path: Path | None, verbose: bool) -> None:
    """Validate genre pack directory structure against pack_schema.yaml."""
    from sidequest.cli.validate.common import packs_in

    if schema_path is None:
        try:
            schema_path = _find_default_schema(pack_dir)
        except FileNotFoundError as exc:
            click.echo(f"ERROR: {exc}", err=True)
            sys.exit(1)

    packs = packs_in(pack_dir)
    if not packs:
        click.echo(f"ERROR: no genre packs found under '{pack_dir}'", err=True)
        sys.exit(1)

    overall_pass = True
    for pack in packs:
        errors, warnings = validate_pack_structure(pack, schema_path)
        n_err = len(errors)
        n_warn = len(warnings)
        status = "PASS" if not errors else "FAIL"
        if errors:
            overall_pass = False

        pad = max(0, 40 - len(pack.name))
        dots = "." * pad
        click.echo(f"{pack.name} {dots} {status} ({n_err} errors, {n_warn} warnings)")
        if verbose or errors:
            for e in errors:
                click.echo(f"  ERROR: {e}")
        if verbose and warnings:
            for w in warnings:
                click.echo(f"  WARN:  {w}")

    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
