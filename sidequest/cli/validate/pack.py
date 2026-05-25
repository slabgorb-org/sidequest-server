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


def _check_required_files(
    directory: Path, required: list[str], label: str
) -> list[str]:
    """Return error strings for each required file missing from ``directory``."""
    errors: list[str] = []
    for fname in required:
        if not (directory / fname).is_file():
            errors.append(f"{label}: missing required file '{fname}'")
    return errors


def _check_required_dirs(
    directory: Path, required: list[str], label: str
) -> list[str]:
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
            errors.append(
                f"{label}: declared extension '{ext_name}' is not defined in schema"
            )
            continue
        for fname in ext_spec.get("files", []):
            if not (directory / fname).is_file():
                errors.append(
                    f"{label}: extension '{ext_name}' requires missing file '{fname}'"
                )
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
# World-level validation
# ---------------------------------------------------------------------------


def _validate_world(
    world_dir: Path,
    world_schema: dict[str, Any],
    genre_schema: dict[str, Any],
    genre_ext_files: set[str],
    genre_ext_dirs: set[str],
    genre_extensions_declared: list[str],
) -> tuple[list[str], list[str]]:
    """Validate a single world directory.

    If ``world.yaml`` contains ``draft: true``, all structural problems are
    demoted to warnings instead of errors.

    Returns ``(errors, warnings)``.
    """
    label = f"world '{world_dir.name}'"

    # Check draft status
    world_yaml_path = world_dir / "world.yaml"
    is_draft = False
    if world_yaml_path.is_file():
        try:
            raw = yaml.safe_load(world_yaml_path.read_text(encoding="utf-8")) or {}
            is_draft = bool(raw.get("draft", False))
        except (yaml.YAMLError, UnicodeDecodeError):
            pass  # Treat unreadable world.yaml as non-draft; missing file handled below

    world_required_files: list[str] = world_schema.get("required_files", [])
    world_required_dirs: list[str] = world_schema.get("required_dirs", [])
    world_extensions_schema: dict[str, Any] = world_schema.get("extensions", {})

    # World doesn't have its own extensions declared in world.yaml (no schema field
    # for that today), so we only check the world's required files/dirs.
    # If a world needs extension files, they're checked via the genre pack's
    # extensions_declared list against world_extensions_schema.
    world_ext_files, world_ext_dirs = _resolve_extension_paths(
        genre_extensions_declared, world_extensions_schema
    )

    structural_errors: list[str] = []
    structural_errors.extend(
        _check_required_files(world_dir, world_required_files, label)
    )
    structural_errors.extend(
        _check_required_dirs(world_dir, world_required_dirs, label)
    )
    structural_errors.extend(
        _check_extensions(
            world_dir, genre_extensions_declared, world_extensions_schema, label
        )
    )

    orphan_warnings = _check_orphans(
        directory=world_dir,
        required_files=world_required_files,
        required_dirs=world_required_dirs,
        extension_files=world_ext_files,
        extension_dirs=world_ext_dirs,
        genre_required_files=[],
        genre_extension_files=set(),
        label=label,
    )

    if is_draft:
        # Demote structural errors to warnings for draft worlds
        return [], structural_errors + orphan_warnings
    else:
        return structural_errors, orphan_warnings


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def validate_pack_structure(
    pack_dir: Path, schema_path: Path
) -> tuple[list[str], list[str]]:
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
        _check_extensions(
            pack_dir, extensions_declared, genre_extensions_schema, label
        )
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
        f"Could not locate pack_schema.yaml from '{pack_dir}'. "
        "Pass --schema explicitly."
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
