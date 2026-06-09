"""RED — Story 96-1: visibility-coverage check moves into the pack validator.

``tests/game/projection/test_visibility_tag_rule.py`` used to assert, per
LIVE shipping pack, that ``projection.yaml`` routes NARRATION and SECRET_NOTE
through a ``visibility_tag`` rule. That is a CONTENT requirement, not an
engine behavior — per the epic 96 doctrine ("validators validate content,
tests test fixtures") the live-pack sweep was deleted from the server suite
and the requirement moves here, into the ``pf validate pack`` content gate,
where it runs against whatever pack the operator points it at.

Contract pinned for Dev (GREEN):

  ``sidequest.game.projection.validator.validate_visibility_coverage(
      rules: ProjectionRules) -> list[str]``

  - Returns one human-readable finding per missing route: a projection rule
    set must carry a ``visibility_tag`` rule for kind=NARRATION and one for
    kind=SECRET_NOTE (the structural-hiding pair — without them the
    ProjectionFilter pass-through leaks per-recipient dispatches).
  - Each finding names the missing kind and the string "visibility_tag" so
    operators can act on it.
  - Empty list = covered.
  - It does NOT raise — coverage gaps are content findings, not load errors.
    (``validate_projection_rules`` stays as-is: it runs at pack LOAD time and
    must not start rejecting minimal fixture packs that opt out of
    projection rules entirely.)

Wiring: ``sidequest.cli.validate.pack._validate_projection`` appends these
findings to the pack validator's error list — but ONLY when projection.yaml
exists. A pack with no projection.yaml at all remains valid (matches the
existing absent-file behavior).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.cli.validate.pack import validate_pack_structure
from sidequest.game.projection.rules import load_rules_from_yaml_str

# Reuse the sibling module's minimal-pack builder + real schema path — same
# synthetic-pack-in-tmp_path pattern, no live content packs.
from tests.cli.validate.test_pack_validator import _minimal_pack, schema_path_real

_FULL_COVERAGE_YAML = """
rules:
  - kind: NARRATION
    visibility_tag: {}
  - kind: SECRET_NOTE
    visibility_tag: {}
"""

_NARRATION_ONLY_YAML = """
rules:
  - kind: NARRATION
    visibility_tag: {}
"""

_SECRET_NOTE_ONLY_YAML = """
rules:
  - kind: SECRET_NOTE
    visibility_tag: {}
"""

_EMPTY_RULES_YAML = """
rules: []
"""


def _coverage(yaml_str: str) -> list[str]:
    from sidequest.game.projection.validator import validate_visibility_coverage

    return validate_visibility_coverage(load_rules_from_yaml_str(yaml_str))


# ---------------------------------------------------------------------------
# Unit contract — validate_visibility_coverage
# ---------------------------------------------------------------------------


def test_full_coverage_returns_no_findings():
    assert _coverage(_FULL_COVERAGE_YAML) == []


def test_missing_secret_note_rule_is_reported():
    findings = _coverage(_NARRATION_ONLY_YAML)
    assert len(findings) == 1, f"expected exactly one finding, got {findings!r}"
    assert "SECRET_NOTE" in findings[0]
    assert "visibility_tag" in findings[0]


def test_missing_narration_rule_is_reported():
    findings = _coverage(_SECRET_NOTE_ONLY_YAML)
    assert len(findings) == 1, f"expected exactly one finding, got {findings!r}"
    assert "NARRATION" in findings[0]
    assert "visibility_tag" in findings[0]


def test_empty_rules_reports_both_kinds():
    findings = _coverage(_EMPTY_RULES_YAML)
    assert len(findings) == 2, f"expected two findings, got {findings!r}"
    joined = "\n".join(findings)
    assert "NARRATION" in joined
    assert "SECRET_NOTE" in joined


def test_non_visibility_rule_for_kind_does_not_count_as_coverage():
    """A NARRATION rule of a DIFFERENT type must not satisfy the requirement —
    coverage means a visibility_tag rule specifically (the pass-through-leak
    guard), not any rule mentioning the kind."""
    yaml_str = """
rules:
  - kind: NARRATION
    redact_fields:
      - field: text
        unless: is_self(text)
        mask: "**"
  - kind: SECRET_NOTE
    visibility_tag: {}
"""
    findings = _coverage(yaml_str)
    assert len(findings) == 1, f"expected one finding for NARRATION, got {findings!r}"
    assert "NARRATION" in findings[0]


# ---------------------------------------------------------------------------
# Wiring — the pack validator reports coverage findings (96-1 wiring test)
# ---------------------------------------------------------------------------

pytestmark_schema = pytest.mark.skipif(
    not schema_path_real.is_file(),
    reason="pack_schema.yaml not on disk (sidequest-content checkout missing)",
)


def _visibility_errors(errors: list[str]) -> list[str]:
    return [e for e in errors if "visibility_tag" in e]


@pytestmark_schema
def test_pack_validator_reports_missing_visibility_coverage(tmp_path: Path) -> None:
    pack_dir = _minimal_pack(tmp_path)
    (pack_dir / "projection.yaml").write_text(_NARRATION_ONLY_YAML, encoding="utf-8")

    errors, _warnings = validate_pack_structure(pack_dir, schema_path_real)
    hits = _visibility_errors(errors)
    assert hits, (
        "pack validator must report a projection visibility-coverage finding "
        f"when SECRET_NOTE has no visibility_tag rule; errors were: {errors!r}"
    )
    assert any("SECRET_NOTE" in e for e in hits)


@pytestmark_schema
def test_pack_validator_passes_full_visibility_coverage(tmp_path: Path) -> None:
    pack_dir = _minimal_pack(tmp_path)
    (pack_dir / "projection.yaml").write_text(_FULL_COVERAGE_YAML, encoding="utf-8")

    errors, _warnings = validate_pack_structure(pack_dir, schema_path_real)
    assert _visibility_errors(errors) == [], (
        f"full coverage must produce no visibility findings; errors: {errors!r}"
    )


@pytestmark_schema
def test_pack_without_projection_yaml_has_no_visibility_findings(tmp_path: Path) -> None:
    """Absent projection.yaml stays valid — the coverage check only applies
    when a pack opts into projection rules (matches _validate_projection's
    existing absent-file behavior)."""
    pack_dir = _minimal_pack(tmp_path)
    projection = pack_dir / "projection.yaml"
    if projection.exists():  # _minimal_pack does not create it today; guard drift
        projection.unlink()

    errors, _warnings = validate_pack_structure(pack_dir, schema_path_real)
    assert _visibility_errors(errors) == [], (
        f"no projection.yaml must mean no visibility findings; errors: {errors!r}"
    )


def test_validate_projection_rules_still_accepts_uncovered_rules():
    """Load-time validation must NOT grow the coverage requirement: a minimal
    pack with (say) only a NARRATION rule still loads. The coverage check is
    validator-CLI-only severity (content gate), never a load gate."""
    from sidequest.game.projection.validator import validate_projection_rules

    rules = load_rules_from_yaml_str(_NARRATION_ONLY_YAML)
    # Must not raise — if this starts raising, the loader would reject every
    # minimal fixture pack and the blast radius is the whole suite.
    validate_projection_rules(rules)
