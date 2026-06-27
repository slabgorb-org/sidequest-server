"""``pf validate rules`` — genre-pack confrontation-rules invariants.

Validates ``rules.yaml`` confrontation definitions against ADR-153 firewall
doctrine. Currently one check:

* ``SEALED_LETTER_COMBAT_NOT_HP_DEPLETION`` (error) — a ``sealed_letter_lookup``
  *combat* confrontation must resolve via ``win_condition: hp_depletion``. A
  sealed-letter dogfight is bound-ruleset combat (SOUL.md "Bind the Ruleset,
  Don't Balance It"); it must NOT carry a native dial (``dial_threshold``). This
  guards the 158-31 contradiction (``sealed_letter_lookup`` + ``dial_threshold``)
  so it can never reappear in content.

* ``RULES_LOAD_FAILURE`` (error) — ``rules.yaml`` failed to parse. Reported here
  (not allowed to crash the walk) so a broken pack doesn't suppress siblings.

A missing ``rules.yaml`` is silently OK (text-only / stub packs); the loader
catches that at server startup, not the validator's concern. Reads the YAML raw
(no model load), matching ``validate.audio._check_rules_moods`` — a confrontation
whose *other* fields are malformed must still be checked for this contradiction.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import click
import yaml

from sidequest.cli.validate.common import packs_in

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


def _check_confrontation_firewall(pack_dir: Path, result: ValidationResult) -> None:
    """Flag any ``sealed_letter_lookup`` combat confrontation that does not
    resolve via ``hp_depletion`` (ADR-153 §2 firewall / finding 158-31)."""
    rules_path = pack_dir / "rules.yaml"
    if not rules_path.is_file():
        return
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        result.record(
            Issue(
                code="RULES_LOAD_FAILURE",
                severity="error",
                message=str(exc),
                pack=pack_dir.name,
                file="rules.yaml",
            )
        )
        return
    confrontations = raw.get("confrontations") or []
    for conf in confrontations:
        if not isinstance(conf, dict):
            continue
        if conf.get("resolution_mode") != "sealed_letter_lookup":
            continue
        if conf.get("category") != "combat":
            continue
        # A missing win_condition defaults to dial_threshold at model load
        # (ConfrontationDef), so an absent value is the contradiction too — the
        # firewall requires hp_depletion to be explicit.
        win_condition = conf.get("win_condition")
        if win_condition == "hp_depletion":
            continue
        conf_id = conf.get("type") or conf.get("id") or conf.get("label") or "<unknown>"
        result.record(
            Issue(
                code="SEALED_LETTER_COMBAT_NOT_HP_DEPLETION",
                severity="error",
                message=(
                    f"confrontation {conf_id!r}: sealed_letter_lookup combat must use "
                    f"win_condition=hp_depletion (ADR-153 firewall — bound ruleset owns "
                    f"hull/hit/kill, no native dial), got {win_condition!r}"
                ),
                pack=pack_dir.name,
                file="rules.yaml",
            )
        )


def validate_rules_in_pack(pack_dir: Path) -> ValidationResult:
    """Per-pack programmatic entry. Returns the accumulated diagnostics.

    Returns an empty result immediately if ``rules.yaml`` is absent.
    """
    result = ValidationResult()
    _check_confrontation_firewall(pack_dir, result)
    return result


def validate_packs(pack_roots: list[Path]) -> ValidationResult:
    """Multi-pack entry — walks every pack found under each given root.

    Per-pack failures must not suppress sibling packs: each pack's diagnostics
    accumulate into the same result regardless of any other pack's outcome.
    """
    result = ValidationResult()
    for root in pack_roots:
        for pack in packs_in(root):
            per_pack = validate_rules_in_pack(pack)
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
    """Validate confrontation-rules invariants across every wired genre pack."""
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
            f"rules: {len(result.errors)} errors, {len(result.warnings)} warnings",
            err=True,
        )

    ctx.exit(0 if result.success else 1)


# Allow ``python -m sidequest.cli.validate.rules`` direct entry (parity with the
# sibling validators) in addition to the ``validate rules`` group subcommand.
if __name__ == "__main__":
    main()


__all__ = ["Issue", "ValidationResult", "validate_rules_in_pack", "validate_packs", "main"]
