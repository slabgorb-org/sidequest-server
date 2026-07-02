"""``pf validate rules`` — genre-pack confrontation-rules invariants.

Validates ``rules.yaml`` confrontation definitions against ADR-153 doctrine:

* ``SEALED_LETTER_COMBAT_NOT_HP_DEPLETION`` (error) — a ``sealed_letter_lookup``
  *combat* confrontation must resolve via ``win_condition: hp_depletion``. A
  sealed-letter dogfight is bound-ruleset combat (SOUL.md "Bind the Ruleset,
  Don't Balance It"); it must NOT carry a native dial (``dial_threshold``). This
  guards the 158-31 contradiction (``sealed_letter_lookup`` + ``dial_threshold``)
  so it can never reappear in content.

* The ADR-153 §3 state-graph invariants (story 158-40) — see
  ``_check_state_graph``: dangling ``next_state`` transitions, unreachable
  state tables, non-descriptor keys inside cell views (the §2 firewall), and
  descriptor-schema ↔ table-registry mvp consistency.

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


# Descriptor fields a positioning-cell view may write (ADR-153 §2 firewall).
# Mirrors dogfight/descriptor_schema.yaml's field vocabulary (mvp + future —
# the schema is deliberately genre-agnostic). Hull/hit/damage belong to the
# bound ruleset and must never appear in a view; the pydantic cell model can't
# catch this (views are open dicts), so the validator is the enforcement point.
_ALLOWED_VIEW_KEYS = frozenset(
    {
        "target_bearing",
        "target_range",
        "target_aspect",
        "closure",
        "viewer_energy",
        "target_energy",
        "gun_solution",
        "environment",
        "narration_style",
        "target_elevation",
        "target_bank",
        "missile_lock",
        "viewer_bank",
    }
)


def _resolve_raw_state_tables(
    conf: dict,
    conf_id: str,
    pack_dir: Path,
    result: ValidationResult,
) -> dict[str, dict]:
    """Resolve a raw ``interaction_tables`` list (inline tables or
    ``{_from: relpath}`` pointers) into ``{starting_state: table_dict}``,
    recording issues for unreadable side-files or missing keys."""
    tables: dict[str, dict] = {}
    for entry in conf.get("interaction_tables") or []:
        table = entry
        if isinstance(entry, dict) and set(entry) == {"_from"}:
            side_path = pack_dir / str(entry["_from"])
            try:
                table = yaml.safe_load(side_path.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                result.record(
                    Issue(
                        code="STATE_GRAPH_TABLE_UNREADABLE",
                        severity="error",
                        message=(
                            f"confrontation {conf_id!r}: interaction_tables entry "
                            f"{entry['_from']!r} could not be read: {exc}"
                        ),
                        pack=pack_dir.name,
                        file="rules.yaml",
                    )
                )
                continue
        if not isinstance(table, dict) or not table.get("starting_state"):
            result.record(
                Issue(
                    code="STATE_GRAPH_TABLE_MISSING_STATE",
                    severity="error",
                    message=(
                        f"confrontation {conf_id!r}: interaction_tables entry has no "
                        f"starting_state (entry: {entry!r})"
                    ),
                    pack=pack_dir.name,
                    file="rules.yaml",
                )
            )
            continue
        tables[str(table["starting_state"])] = table
    return tables


def _check_state_graph(pack_dir: Path, result: ValidationResult) -> None:
    """ADR-153 §3 state-graph invariants (story 158-40) for any confrontation
    declaring ``interaction_tables``:

    * ``STATE_GRAPH_DANGLING_NEXT_STATE`` (error) — a cell transitions to a
      state with no registered table (the duel would fail loud mid-flight).
    * ``STATE_GRAPH_UNREACHABLE_STATE`` (error) — a registered table no
      transition reaches from the entry state: dead content (No Stubbing).
    * ``STATE_GRAPH_VIEW_KEY_FIREWALL`` (error) — a cell view carries a
      non-descriptor key (e.g. ``damage``): the §2 firewall violation the
      cell model cannot catch because views are open dicts.
    * ``STATE_GRAPH_SCHEMA_MISMATCH`` (error) — descriptor_schema.yaml
      ``status: mvp`` starting-states and the table registry must match
      one-to-one (a promoted state with no table is a stub; a table whose
      state the schema doesn't promote is undeclared content).
    """
    rules_path = pack_dir / "rules.yaml"
    if not rules_path.is_file():
        return
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        return  # RULES_LOAD_FAILURE already recorded by the firewall check
    for conf in raw.get("confrontations") or []:
        if not isinstance(conf, dict) or not conf.get("interaction_tables"):
            continue
        conf_id = conf.get("type") or conf.get("id") or conf.get("label") or "<unknown>"
        tables = _resolve_raw_state_tables(conf, str(conf_id), pack_dir, result)
        if not tables:
            continue
        states = set(tables)
        entry_state = next(iter(tables))

        # -- closure + firewall, per cell ---------------------------------
        transitions: dict[str, set[str]] = {state: set() for state in states}
        for state, table in tables.items():
            for cell in table.get("cells") or []:
                if not isinstance(cell, dict):
                    continue
                pair = cell.get("pair")
                next_state = cell.get("next_state")
                if next_state:
                    if next_state in states:
                        transitions[state].add(str(next_state))
                    else:
                        result.record(
                            Issue(
                                code="STATE_GRAPH_DANGLING_NEXT_STATE",
                                severity="error",
                                message=(
                                    f"confrontation {conf_id!r} state {state!r}: cell "
                                    f"{pair!r} next_state {next_state!r} has no "
                                    f"registered interaction table "
                                    f"(states: {sorted(states)})"
                                ),
                                pack=pack_dir.name,
                                file="rules.yaml",
                            )
                        )
                for view_key in ("red_view", "blue_view"):
                    view = cell.get(view_key)
                    if not isinstance(view, dict):
                        continue
                    for key in sorted(set(view) - _ALLOWED_VIEW_KEYS):
                        result.record(
                            Issue(
                                code="STATE_GRAPH_VIEW_KEY_FIREWALL",
                                severity="error",
                                message=(
                                    f"confrontation {conf_id!r} state {state!r}: cell "
                                    f"{pair!r} {view_key} carries non-descriptor key "
                                    f"{key!r} — positioning cells carry geometry + "
                                    f"gun_solution only (ADR-153 §2 firewall; the bound "
                                    f"ruleset owns damage)"
                                ),
                                pack=pack_dir.name,
                                file="rules.yaml",
                            )
                        )

        # -- reachability from the entry state ----------------------------
        # extend-and-return resets toward merge at runtime, but authored
        # reachability must hold via cell transitions alone: a table nothing
        # transitions into is dead content.
        reached = {entry_state}
        frontier = [entry_state]
        while frontier:
            for target in transitions.get(frontier.pop(), ()):
                if target not in reached:
                    reached.add(target)
                    frontier.append(target)
        for state in sorted(states - reached):
            result.record(
                Issue(
                    code="STATE_GRAPH_UNREACHABLE_STATE",
                    severity="error",
                    message=(
                        f"confrontation {conf_id!r}: state {state!r} is not reachable "
                        f"from entry state {entry_state!r} — no cell transitions into "
                        f"it (dead content)"
                    ),
                    pack=pack_dir.name,
                    file="rules.yaml",
                )
            )

        # -- descriptor-schema ↔ registry consistency ----------------------
        schema_path = pack_dir / "dogfight" / "descriptor_schema.yaml"
        if not schema_path.is_file():
            continue
        try:
            schema = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            result.record(
                Issue(
                    code="STATE_GRAPH_SCHEMA_MISMATCH",
                    severity="error",
                    message=(
                        f"confrontation {conf_id!r}: dogfight/descriptor_schema.yaml "
                        f"could not be read for state-graph consistency: {exc}"
                    ),
                    pack=pack_dir.name,
                    file="dogfight/descriptor_schema.yaml",
                )
            )
            continue
        mvp_states = {
            str(s.get("id"))
            for s in schema.get("starting_states") or []
            if isinstance(s, dict) and s.get("status") == "mvp" and s.get("id")
        }
        for state in sorted(mvp_states - states):
            result.record(
                Issue(
                    code="STATE_GRAPH_SCHEMA_MISMATCH",
                    severity="error",
                    message=(
                        f"confrontation {conf_id!r}: descriptor_schema starting_state "
                        f"{state!r} is status=mvp but has no interaction table in "
                        f"interaction_tables (a promoted state with no table is a stub)"
                    ),
                    pack=pack_dir.name,
                    file="dogfight/descriptor_schema.yaml",
                )
            )
        for state in sorted(states - mvp_states):
            result.record(
                Issue(
                    code="STATE_GRAPH_SCHEMA_MISMATCH",
                    severity="error",
                    message=(
                        f"confrontation {conf_id!r}: interaction table {state!r} has no "
                        f"status=mvp starting_state in dogfight/descriptor_schema.yaml — "
                        f"promote the schema state in the same change"
                    ),
                    pack=pack_dir.name,
                    file="dogfight/descriptor_schema.yaml",
                )
            )


def validate_rules_in_pack(pack_dir: Path) -> ValidationResult:
    """Per-pack programmatic entry. Returns the accumulated diagnostics.

    Returns an empty result immediately if ``rules.yaml`` is absent.
    """
    result = ValidationResult()
    _check_confrontation_firewall(pack_dir, result)
    _check_state_graph(pack_dir, result)
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
