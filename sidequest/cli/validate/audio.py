"""``pf validate audio`` — audio.yaml + rules.yaml mood-reference audit.

Story 50-9 / ADR-033 Pillar 3 Steps 1-3.

Two checks:

1. **Pack load (hard error):** building ``AudioConfig`` from ``audio.yaml``
   must succeed. The pydantic ``_validate_mood_aliases`` validator already
   rejects declared alias chains that cycle, break, or exceed depth — this
   validator catches that loud ValueError and wraps it as an
   ``AUDIO_LOAD_FAILURE`` issue so the audit keeps walking sibling packs
   instead of crashing on the first bad one.

2. **Rules mood resolution (warning):** every ``confrontations[*].mood``
   in ``rules.yaml`` must resolve to a ``mood_tracks`` key — either directly
   or via the ``mood_aliases`` chain. References that don't resolve fall to
   the universal ``exploration`` fallback at runtime with a
   ``music.mood_alias_failed`` span; surfacing them here lets content
   authors add an alias before the silent-ish fallback ships.

Severity choice: ``UNRESOLVED_RULES_MOOD`` is a warning, not an error.
Runtime has a graceful, observable fallback; hard-erroring at validate
time would block ship on any narrator-likely mood a content author hasn't
anticipated. ``AUDIO_LOAD_FAILURE`` IS an error because the pack will
refuse to load at server startup.

The runtime track-selection resolver (``resolve_mood_to_track_key`` in
``sidequest/audio/library_backend.py``) emits OTEL spans as a side effect
of its happy-path observability. The audit walks aliases via a pure helper
(``_chain_resolves_to_track``) so running the validator from CI / a
content-team laptop does not pollute traces with hundreds of resolution
spans on every invocation.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import click
import yaml

from sidequest.genre.models.audio import MAX_ALIAS_HOPS, AudioConfig

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str
    pack: str
    file: str


@dataclass
class ValidationResult:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    def record(self, issue: Issue) -> None:
        (self.errors if issue.severity == "error" else self.warnings).append(issue)

    @property
    def success(self) -> bool:
        return not self.errors


def _chain_resolves_to_track(mood: str, tracks: dict[str, Any], aliases: dict[str, str]) -> bool:
    """Pure check: does ``mood`` resolve to a ``tracks`` key, directly or via
    the alias chain? Mirrors ``resolve_mood_to_track_key`` but without the
    OTEL span side effects (this runs in audit context, not turn context)."""
    if mood in tracks:
        return True
    if mood not in aliases:
        return False
    seen = {mood}
    cur = aliases[mood]
    depth = 1
    while cur not in tracks:
        if cur in seen or cur not in aliases or depth >= MAX_ALIAS_HOPS:
            return False
        seen.add(cur)
        cur = aliases[cur]
        depth += 1
    return True


def _packs_in(root: Path) -> list[Path]:
    """Return every directory under ``root`` that looks like a genre pack.

    Two shapes are accepted: ``root`` is itself a pack (``pack.yaml``
    present at ``root``), or ``root`` is a directory containing many packs
    (each child with its own ``pack.yaml``). Mirrors
    ``locations._packs_in`` so the two validators share discovery shape.
    """
    if not root.is_dir():
        return []
    if (root / "pack.yaml").is_file():
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "pack.yaml").is_file())


def _load_audio_config(pack_dir: Path, result: ValidationResult) -> AudioConfig | None:
    """Construct an ``AudioConfig`` from ``pack_dir/audio.yaml``.

    On ValueError (the pydantic ``_validate_mood_aliases`` loud failure for
    declared cycles / broken targets / depth-exceeded chains), record an
    ``AUDIO_LOAD_FAILURE`` error naming the offending alias and return
    ``None`` so the per-pack walk skips the mood-resolution check that
    needs a constructed config.
    """
    path = pack_dir / "audio.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    try:
        return AudioConfig.model_validate(raw)
    except ValueError as exc:
        result.record(
            Issue(
                code="AUDIO_LOAD_FAILURE",
                severity="error",
                message=str(exc),
                pack=pack_dir.name,
                file="audio.yaml",
            )
        )
        return None


def _check_rules_moods(pack_dir: Path, cfg: AudioConfig, result: ValidationResult) -> None:
    """Scan ``rules.yaml`` for confrontation moods that don't resolve to a
    track (directly or via the alias chain). Each unresolved mood produces
    one ``UNRESOLVED_RULES_MOOD`` warning.
    """
    rules_path = pack_dir / "rules.yaml"
    if not rules_path.is_file():
        return
    raw = yaml.safe_load(rules_path.read_text()) or {}
    confrontations = raw.get("confrontations") or []
    for conf in confrontations:
        if not isinstance(conf, dict):
            continue
        mood = conf.get("mood")
        if not isinstance(mood, str) or not mood:
            continue
        if _chain_resolves_to_track(mood, cfg.mood_tracks, cfg.mood_aliases):
            continue
        # confrontation schemas vary across packs: dungeon/social packs use
        # `id` + `name`, magic-system packs use `type` + `label`. Fall through
        # so the triage label is useful regardless of which shape the pack
        # author chose.
        conf_id = (
            conf.get("id")
            or conf.get("type")
            or conf.get("label")
            or conf.get("name")
            or "<unknown>"
        )
        result.record(
            Issue(
                code="UNRESOLVED_RULES_MOOD",
                severity="warning",
                message=(
                    f"confrontation {conf_id!r} references mood {mood!r} which "
                    f"is neither a mood_tracks key nor a declared alias that "
                    f"resolves to one — will fall back to 'exploration' at "
                    f"runtime"
                ),
                pack=pack_dir.name,
                file="rules.yaml",
            )
        )


def validate_audio_in_pack(pack_dir: Path) -> ValidationResult:
    """Per-pack programmatic entry. Runs both checks on a single pack
    directory and returns the accumulated diagnostics."""
    result = ValidationResult()
    if not (pack_dir / "audio.yaml").is_file():
        return result
    cfg = _load_audio_config(pack_dir, result)
    if cfg is None:
        return result
    _check_rules_moods(pack_dir, cfg, result)
    return result


def validate_packs(pack_roots: list[Path]) -> ValidationResult:
    """Multi-pack entry — walks every pack found under each given root.

    Per-pack failures must not suppress sibling packs: each pack's
    diagnostics are accumulated into the same result regardless of any
    other pack's outcome.
    """
    result = ValidationResult()
    for root in pack_roots:
        for pack in _packs_in(root):
            per_pack = validate_audio_in_pack(pack)
            for issue in per_pack.errors:
                result.record(issue)
            for issue in per_pack.warnings:
                result.record(issue)
    return result


@click.command()
@click.option(
    "--genre-packs-root",
    "roots",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Genre-pack directory or single pack root. May be passed multiple times.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_context
def main(ctx: click.Context, roots: tuple[Path, ...], as_json: bool) -> None:
    """Validate audio.yaml mood references across every wired genre pack."""
    if not roots:
        from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS

        roots = tuple(DEFAULT_GENRE_PACK_SEARCH_PATHS)

    result = validate_packs(list(roots))

    if as_json:
        payload = {
            "passed": result.success,
            "errors": [asdict(i) for i in result.errors],
            "warnings": [asdict(i) for i in result.warnings],
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        for issue in result.errors:
            click.echo(f"[ERROR] {issue.code} {issue.pack}/{issue.file}: {issue.message}", err=True)
        for issue in result.warnings:
            click.echo(f"[WARN] {issue.code} {issue.pack}/{issue.file}: {issue.message}", err=True)
        click.echo(
            f"audio: {len(result.errors)} errors, {len(result.warnings)} warnings",
            err=True,
        )

    ctx.exit(0 if result.success else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
