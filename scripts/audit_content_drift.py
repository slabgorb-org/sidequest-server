"""Audit every genre pack against the Python strict schema.

Loads each top-level YAML in every genre pack directly against its Pydantic
model with `extra: forbid`. Collects every ValidationError without
short-circuiting on the first failure. Produces a drift report suitable
for triaging content-vs-model mismatches in the ADR-082 port.

Rust loaded these same YAMLs with `#[serde(deny_unknown_fields)]` only on
the outer GenrePack, so inner-struct drift (extra fields, renamed fields,
different shapes per genre) accumulated silently. This script surfaces all
of it in one run.

Usage:
    cd sidequest-server
    uv run python scripts/audit_content_drift.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from sidequest.genre.models.archetype_constraints import ArchetypeConstraints
from sidequest.genre.models.audio import AudioConfig, VoicePresets
from sidequest.genre.models.axes import AxesConfig
from sidequest.genre.models.character import (
    BackstoryTables,
    CharCreationScene,
    EquipmentTables,
    NpcArchetype,
    VisualStyle,
)
from sidequest.genre.models.culture import Culture
from sidequest.genre.models.inventory import InventoryConfig
from sidequest.genre.models.lore import Lore
from sidequest.genre.models.narrative import (
    Achievement,
    BeatVocabulary,
    OpeningHook,
    Prompts,
)
from sidequest.genre.models.ocean import DramaThresholds
from sidequest.genre.models.pack import PackMeta
from sidequest.genre.models.progression import ProgressionConfig
from sidequest.genre.models.rules import RulesConfig
from sidequest.genre.models.theme import GenreTheme
from sidequest.genre.models.tropes import TropeDefinition

REPO_ROOT = Path(__file__).resolve().parents[2]
GENRE_PACKS = REPO_ROOT / "sidequest-content" / "genre_packs"

# (filename, model, repeated?) — repeated=True means the file is a top-level list
# where each item validates against the model; False means the whole file validates.
REQUIRED: list[tuple[str, type[BaseModel], bool]] = [
    ("pack.yaml", PackMeta, False),
    ("rules.yaml", RulesConfig, False),
    ("lore.yaml", Lore, False),
    ("theme.yaml", GenreTheme, False),
    ("archetypes.yaml", NpcArchetype, True),
    ("char_creation.yaml", CharCreationScene, True),
    ("visual_style.yaml", VisualStyle, False),
    ("progression.yaml", ProgressionConfig, False),
    ("axes.yaml", AxesConfig, False),
    ("audio.yaml", AudioConfig, False),
    ("cultures.yaml", Culture, True),
    ("prompts.yaml", Prompts, False),
    ("tropes.yaml", TropeDefinition, True),
]

OPTIONAL: list[tuple[str, type[BaseModel], bool]] = [
    ("achievements.yaml", Achievement, True),
    ("beat_vocabulary.yaml", BeatVocabulary, False),
    ("voice_presets.yaml", VoicePresets, False),
    ("pacing.yaml", DramaThresholds, False),
    ("inventory.yaml", InventoryConfig, False),
    ("openings.yaml", OpeningHook, True),
    ("backstory_tables.yaml", BackstoryTables, False),
    ("equipment_tables.yaml", EquipmentTables, False),
    ("archetype_constraints.yaml", ArchetypeConstraints, False),
]


def _validate_one(path: Path, model: type[BaseModel], repeated: bool) -> list[str]:
    """Return a list of human-readable error lines for this file.

    Empty list means the file validates cleanly.
    """
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return [f"{path.name}: YAML parse error: {e}"]

    errors: list[str] = []
    if repeated:
        if not isinstance(raw, list):
            return [f"{path.name}: expected top-level list, got {type(raw).__name__}"]
        for idx, item in enumerate(raw):
            try:
                model.model_validate(item)
            except ValidationError as e:
                for err in e.errors():
                    loc = ".".join(str(p) for p in err["loc"])
                    errors.append(f"{path.name}[{idx}].{loc}: {err['type']} — {err['msg']}")
    else:
        try:
            model.model_validate(raw)
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(p) for p in err["loc"])
                errors.append(f"{path.name}.{loc}: {err['type']} — {err['msg']}")
    return errors


def _audit_pack(pack_path: Path) -> dict[str, list[str]]:
    """Return {filename: [errors]} for one genre pack."""
    out: dict[str, list[str]] = {}
    for fname, model, repeated in REQUIRED + OPTIONAL:
        errs = _validate_one(pack_path / fname, model, repeated)
        if errs:
            out[fname] = errs
    return out


def _summarize_error_types(all_errors: dict[str, dict[str, list[str]]]) -> dict[str, int]:
    """Group errors by (file, field path, error type) to surface systemic drift."""
    counts: dict[str, int] = {}
    for _pack, files in all_errors.items():
        for _fname, errs in files.items():
            for err in errs:
                # strip list indices so "archetypes.yaml[0]" and "[1]" collapse
                import re

                key = re.sub(r"\[\d+\]", "[N]", err)
                counts[key] = counts.get(key, 0) + 1
    return counts


def _collect_raw_errors(pack_path: Path) -> list[tuple[str, str, dict[str, Any]]]:
    """Collect per-file raw ValidationError dicts for markdown-table triage.

    Returns list of (filename, canonical_field_path, error_dict). Repeated errors
    across list items are collapsed (e.g., archetypes[0], [1] → archetypes[N]).
    """
    import re

    raw: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[tuple[str, str, str]] = set()

    def _collect(path: Path, model: type[BaseModel], repeated: bool) -> None:
        if not path.exists():
            return
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return

        def _record(err: dict[str, Any], idx_prefix: str = "") -> None:
            loc = ".".join(str(p) for p in err["loc"])
            canonical = re.sub(r"\.\d+\.", ".[N].", f"{idx_prefix}{loc}")
            canonical = re.sub(r"^\d+\.", "[N].", canonical)
            canonical = re.sub(r"\.\d+$", ".[N]", canonical)
            key = (path.name, canonical, err["type"])
            if key in seen:
                return
            seen.add(key)
            raw.append((path.name, canonical, err))

        if repeated and isinstance(loaded, list):
            for _idx, item in enumerate(loaded):
                try:
                    model.model_validate(item)
                except ValidationError as e:
                    for err in e.errors():
                        _record(err, idx_prefix="[N].")
        else:
            try:
                model.model_validate(loaded)
            except ValidationError as e:
                for err in e.errors():
                    _record(err)

    for fname, model, repeated in REQUIRED + OPTIONAL:
        _collect(pack_path / fname, model, repeated)
    return raw


def _example_value(err: dict[str, Any]) -> str:
    """Format the input_value from a Pydantic error for the triage table."""
    v = err.get("input")
    if v is None:
        return ""
    s = repr(v) if not isinstance(v, str) else v
    if len(s) > 60:
        s = s[:57] + "…"
    return s.replace("|", "\\|").replace("\n", " ")


def _write_triage_table(
    out_path: Path, all_errors_raw: dict[str, list[tuple[str, str, dict[str, Any]]]]
) -> None:
    """Write a markdown triage table with ghost/wire/prose columns.

    Each unique (pack, file, canonical_field_path, error_type) is one row.
    """
    rows: list[tuple[str, str, str, str, str, str]] = []
    for pack_name in sorted(all_errors_raw):
        for fname, field, err in all_errors_raw[pack_name]:
            rows.append(
                (
                    pack_name,
                    fname,
                    field,
                    err["type"],
                    _example_value(err),
                    err["msg"].replace("|", "\\|")[:80],
                )
            )

    lines: list[str] = []
    lines.append("# Content Drift Triage — ADR-082 Python Port")
    lines.append("")
    lines.append(
        "Each row is a YAML field that the strict Python port rejects but the "
        "Rust port silently dropped. Triage each as:"
    )
    lines.append("")
    lines.append(
        "- **ghost** — authored for a feature never built; delete from YAML (or file wiring story)"
    )
    lines.append(
        "- **wire** — engine should consume this; add model field + engine reader + OTEL span"
    )
    lines.append(
        "- **prose** — flavor for LLM prompts only; accept in model as pass-through, no consumer"
    )
    lines.append("")
    lines.append(
        "Mark the correct column with `x`. After triage, run the follow-up scripts "
        "to generate content-delete PRs, model-fix stories, and pass-through fields."
    )
    lines.append("")
    lines.append(f"Total rows: **{len(rows)}**")
    lines.append("")
    lines.append("| Pack | File | Field | Error type | Example value | ghost? | wire? | prose? |")
    lines.append("|------|------|-------|-----------|---------------|:------:|:-----:|:------:|")
    for pack, fname, field, etype, example, _msg in rows:
        lines.append(f"| {pack} | `{fname}` | `{field}` | {etype} | `{example}` |   |   |   |")
    lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    if not GENRE_PACKS.exists():
        print(f"ERROR: genre packs dir not found: {GENRE_PACKS}", file=sys.stderr)
        return 1

    all_errors: dict[str, dict[str, list[str]]] = {}
    all_errors_raw: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    clean: list[str] = []

    for pack_dir in sorted(p for p in GENRE_PACKS.iterdir() if p.is_dir()):
        pack_errs = _audit_pack(pack_dir)
        if pack_errs:
            all_errors[pack_dir.name] = pack_errs
            all_errors_raw[pack_dir.name] = _collect_raw_errors(pack_dir)
        else:
            clean.append(pack_dir.name)

    print("=" * 80)
    print("CONTENT DRIFT AUDIT — Python strict schema vs Rust lossy schema")
    print("=" * 80)
    print()
    print(f"Clean packs ({len(clean)}): {', '.join(clean) if clean else '(none)'}")
    print(f"Drifted packs ({len(all_errors)}):")
    print()

    total_errors = 0
    for pack_name, files in all_errors.items():
        file_total = sum(len(errs) for errs in files.values())
        total_errors += file_total
        print(f"── {pack_name} ({file_total} errors across {len(files)} files)")
        for fname, errs in files.items():
            print(f"   {fname}  ({len(errs)} errors)")
            for err in errs[:3]:
                print(f"     • {err}")
            if len(errs) > 3:
                print(f"     • … and {len(errs) - 3} more in this file")
        print()

    print("=" * 80)
    print(f"TOTAL: {total_errors} validation errors across {len(all_errors)} packs")
    print("=" * 80)
    print()
    print("Systemic drift (same error appearing in multiple packs):")
    print()
    counts = _summarize_error_types(all_errors)
    # only surface errors that repeat across packs
    systemic = sorted(
        ((err, n) for err, n in counts.items() if n > 1),
        key=lambda kv: -kv[1],
    )
    if not systemic:
        print("  (no repeated errors — drift is per-pack)")
    else:
        for err, n in systemic[:30]:
            print(f"  [{n}×]  {err}")

    # Write the triage table to the orchestrator root for Keith to edit.
    triage_path = REPO_ROOT / "docs" / "content-drift-triage.md"
    triage_path.parent.mkdir(parents=True, exist_ok=True)
    _write_triage_table(triage_path, all_errors_raw)
    print()
    print(f"Triage table written to: {triage_path}")

    return 0 if not all_errors else 2


if __name__ == "__main__":
    sys.exit(main())
