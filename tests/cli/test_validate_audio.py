"""Tests for ``pf validate audio`` — Story 50-9 / ADR-033 Pillar 3 Steps 1-3.

**Architectural premise:** the mood_aliases *mechanism* is fully covered by
``tests/audio/test_mood_alias_chain.py`` (21 fixture-driven behavior tests,
all GREEN against the shipped implementation). What's left in the story is
*content correctness* — making sure live packs don't reference moods that
fall silently to the universal ``exploration`` fallback at runtime.

Per project rule (``feedback_no_content_coupled_tests``): server tests do
NOT iterate over ``sidequest-content/genre_packs/`` to assert properties of
that data. The audit lives in a separate validator
(``sidequest/cli/validate/audio.py``) that walks live packs and reports
issues loudly. That validator is invoked by humans / CI; its own behavior
is unit-tested here via purpose-built fixture packs under
``tests/fixtures/validate_audio/<case>/``.

Issue codes (Dev — these are the contract):

- ``AUDIO_LOAD_FAILURE`` (error) — ``audio.yaml`` fails to parse
  (``yaml.YAMLError``) or fails pydantic validation (e.g. a declared
  alias chain that doesn't terminate in a ``mood_tracks`` key). Both
  failures are caught and wrapped as Issues so the validator can keep
  walking sibling packs instead of bombing on the first bad one.
- ``RULES_LOAD_FAILURE`` (error) — same shape for ``rules.yaml`` YAML
  parse failures. Dedicated code so triage output distinguishes the
  offending file.
- ``UNRESOLVED_RULES_MOOD`` (warning) — a confrontation ``mood:`` in
  ``rules.yaml`` is neither a ``mood_tracks`` key nor a declared alias that
  resolves to one. Will fall back to ``exploration`` at runtime with a
  WARNING log + ``music.mood_alias_failed`` span; the validator surfaces
  this *before* runtime so the content author can decide whether to add an
  alias or leave it intentional.

Severity choice: warning, not error. The runtime fallback is observable
and graceful — forcing this to a hard error would block ship on any
narrator-likely mood that a content author hasn't anticipated. Warning is
the right pressure: visible in CI output, but does not gate.

Each fixture pack lives at ``tests/fixtures/validate_audio/<case>/`` and is
shaped so exactly one diagnostic should fire (or none, for the ok cases).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sidequest.cli.validate.audio import (
    Issue,
    ValidationResult,
    validate_audio_in_pack,
    validate_packs,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "validate_audio"
SERVER_ROOT = Path(__file__).resolve().parents[2]


def _pack_result(case: str) -> ValidationResult:
    return validate_audio_in_pack(FIXTURES / case)


# ---------------------------------------------------------------------------
# Baseline — well-formed pack produces no issues
# ---------------------------------------------------------------------------


def test_well_formed_pack_has_no_issues() -> None:
    """``audio_ok`` has every rules.yaml mood directly in mood_tracks → zero
    errors AND zero warnings. Guards against the validator inventing
    diagnostics on clean content."""
    res = _pack_result("audio_ok")
    assert res.errors == [], f"unexpected errors: {[i.code for i in res.errors]}"
    assert res.warnings == [], f"unexpected warnings: {[i.code for i in res.warnings]}"


# ---------------------------------------------------------------------------
# UNRESOLVED_RULES_MOOD — the surface that catches caverns/comedic + tea/mystery
# ---------------------------------------------------------------------------


def test_unresolved_rules_mood_emits_warning() -> None:
    """``audio_unresolved_rules_mood`` declares ``confrontations[*].mood: comedic``
    but ``mood_tracks`` only has exploration + tension and ``mood_aliases`` is
    empty. The validator must emit exactly one UNRESOLVED_RULES_MOOD warning
    naming ``comedic`` — the surface that would catch the live caverns
    ``comedic`` and tea_and_murder ``mystery`` gaps without coupling the
    server test suite to those packs' YAML."""
    res = _pack_result("audio_unresolved_rules_mood")
    unresolved = [i for i in res.warnings if i.code == "UNRESOLVED_RULES_MOOD"]
    assert len(unresolved) == 1, (
        f"expected exactly one UNRESOLVED_RULES_MOOD warning; "
        f"got warnings={[i.code for i in res.warnings]} "
        f"errors={[i.code for i in res.errors]}"
    )
    assert "comedic" in unresolved[0].message
    assert unresolved[0].severity == "warning"
    assert unresolved[0].pack == "audio_unresolved_rules_mood"


def test_unresolved_warning_does_not_become_error() -> None:
    """Severity boundary: an undeclared rules mood must NOT promote to an
    error — runtime has a graceful, observable fallback. Hard-erroring here
    would gate ship on any narrator-likely mood a content author hasn't
    anticipated; warning is the right pressure."""
    res = _pack_result("audio_unresolved_rules_mood")
    assert res.errors == [], (
        f"UNRESOLVED_RULES_MOOD must be warning-only; got errors {[i.code for i in res.errors]}"
    )


# ---------------------------------------------------------------------------
# Alias chains silence the warning — proves the validator walks aliases,
# not just mood_tracks.keys().
# ---------------------------------------------------------------------------


def test_single_hop_alias_silences_unresolved_warning() -> None:
    """``audio_resolved_via_alias`` declares ``comedic: exploration`` and
    references ``comedic`` from rules.yaml. The validator must walk the
    alias chain and treat ``comedic`` as resolved — zero warnings on that
    mood. Guards the validator from a naive ``mood in mood_tracks`` check
    that would re-flag valid aliases."""
    res = _pack_result("audio_resolved_via_alias")
    comedic_warnings = [
        i for i in res.warnings if i.code == "UNRESOLVED_RULES_MOOD" and "comedic" in i.message
    ]
    assert comedic_warnings == [], (
        f"alias 'comedic' -> 'exploration' must silence the unresolved warning; "
        f"got: {[i.message for i in comedic_warnings]}"
    )


def test_multi_hop_alias_chain_silences_unresolved_warning() -> None:
    """``audio_resolved_via_alias`` also declares ``caper: comedic`` (two
    hops). The validator must follow the chain to a real mood_tracks key.
    Guards against a depth-1 lookup that would treat ``caper`` as broken."""
    res = _pack_result("audio_resolved_via_alias")
    caper_warnings = [
        i for i in res.warnings if i.code == "UNRESOLVED_RULES_MOOD" and "caper" in i.message
    ]
    assert caper_warnings == [], (
        f"two-hop alias 'caper' -> 'comedic' -> 'exploration' must silence "
        f"the unresolved warning; got: {[i.message for i in caper_warnings]}"
    )


# ---------------------------------------------------------------------------
# AUDIO_LOAD_FAILURE — declared-but-broken alias becomes an error, not a crash
# ---------------------------------------------------------------------------


def test_broken_declared_alias_becomes_error_not_crash() -> None:
    """``audio_broken_declared_alias`` has ``mood_aliases.comedic =
    nonexistent_target``. ``AudioConfig`` construction will raise loudly
    (the pydantic _validate_mood_aliases validator already catches this).
    The validator must CATCH that ValueError, wrap it as an
    AUDIO_LOAD_FAILURE error Issue, and KEEP WALKING other packs — never
    let one broken pack crash the whole audit."""
    res = _pack_result("audio_broken_declared_alias")
    load_failures = [i for i in res.errors if i.code == "AUDIO_LOAD_FAILURE"]
    assert len(load_failures) == 1, (
        f"expected one AUDIO_LOAD_FAILURE; got errors {[i.code for i in res.errors]}"
    )
    msg = load_failures[0].message
    assert "comedic" in msg and "nonexistent_target" in msg, (
        f"error message must name the offending alias and its broken target; got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Diagnostic quality — code/severity/source-file hygiene
# ---------------------------------------------------------------------------


def test_issue_carries_source_file_for_ide_jumps() -> None:
    """Every diagnostic must reference its source file path — IDE
    jump-to-line + CI grep hygiene, matching the locations validator
    contract."""
    res = _pack_result("audio_unresolved_rules_mood")
    warnings = [i for i in res.warnings if i.code == "UNRESOLVED_RULES_MOOD"]
    assert len(warnings) == 1, (
        f"expected exactly one UNRESOLVED_RULES_MOOD warning; got {len(warnings)}"
    )
    assert all("rules.yaml" in i.file for i in warnings), (
        f"UNRESOLVED_RULES_MOOD must cite rules.yaml; got files: {[i.file for i in warnings]}"
    )


def test_load_failure_cites_audio_yaml() -> None:
    """An AUDIO_LOAD_FAILURE must cite audio.yaml — the file responsible
    for the declared bad alias chain."""
    res = _pack_result("audio_broken_declared_alias")
    failures = [i for i in res.errors if i.code == "AUDIO_LOAD_FAILURE"]
    assert len(failures) == 1, f"expected exactly one AUDIO_LOAD_FAILURE; got {len(failures)}"
    assert all("audio.yaml" in i.file for i in failures), (
        f"AUDIO_LOAD_FAILURE must cite audio.yaml; got files: {[i.file for i in failures]}"
    )


# ---------------------------------------------------------------------------
# Multi-pack walk + isolation
# ---------------------------------------------------------------------------


def test_validate_packs_walks_a_root_of_multiple_packs() -> None:
    """``validate_packs([FIXTURES])`` must enumerate every pack under the
    root, accumulating issues from each. Confirms the multi-pack entry
    point exists and isolates per-pack failures — a broken pack must not
    suppress diagnostics for sibling packs."""
    res = validate_packs([FIXTURES])
    # Every fixture pack contributes its declared diagnostics:
    # - audio_ok: 0 issues
    # - audio_missing: 0 issues (no audio.yaml — legitimate text-only stub)
    # - audio_unresolved_rules_mood: 1 UNRESOLVED_RULES_MOOD warning
    # - audio_resolved_via_alias: 0 issues
    # - audio_broken_declared_alias: 1 AUDIO_LOAD_FAILURE error (pydantic)
    # - audio_malformed_yaml: 1 AUDIO_LOAD_FAILURE error (yaml.YAMLError)
    # - rules_malformed_yaml: 1 RULES_LOAD_FAILURE error
    # See test_validate_packs_does_not_crash_on_any_fixture_root below for
    # the exhaustive assertion across all error families.
    assert any(i.pack == "audio_unresolved_rules_mood" for i in res.warnings), (
        "multi-pack walk must surface the unresolved-mood pack's warning"
    )
    assert any(i.pack == "audio_broken_declared_alias" for i in res.errors), (
        "multi-pack walk must surface the broken-alias pack's error"
    )


def test_one_broken_pack_does_not_suppress_others() -> None:
    """Robustness: AUDIO_LOAD_FAILURE in one pack must not prevent
    UNRESOLVED_RULES_MOOD diagnostics from firing on a different pack in
    the same root. Guards against an early-return on first error."""
    res = validate_packs([FIXTURES])
    assert any(
        i.code == "AUDIO_LOAD_FAILURE" and i.pack == "audio_broken_declared_alias"
        for i in res.errors
    )
    assert any(
        i.code == "UNRESOLVED_RULES_MOOD" and i.pack == "audio_unresolved_rules_mood"
        for i in res.warnings
    )


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_validation_result_success_is_false_when_errors_present() -> None:
    """``ValidationResult.success`` is True iff ``errors`` is empty — same
    contract as the locations validator. Warnings do not flip success."""
    res = _pack_result("audio_unresolved_rules_mood")
    assert res.errors == []
    assert res.warnings != []
    assert res.success is True, "warnings alone must not flip success to False"

    bad = _pack_result("audio_broken_declared_alias")
    assert bad.errors != []
    assert bad.success is False, "presence of any error must flip success to False"


def test_issue_dataclass_carries_required_fields() -> None:
    """``Issue`` is the diagnostic contract: code, severity, message, pack,
    file. Without these, CI output / IDE jumps / triage are useless."""
    res = _pack_result("audio_unresolved_rules_mood")
    issue: Issue = res.warnings[0]
    for field in ("code", "severity", "message", "pack", "file"):
        assert hasattr(issue, field), f"Issue must expose '{field}'"
        assert getattr(issue, field) not in (None, ""), (
            f"Issue.{field} must be populated; got {getattr(issue, field)!r}"
        )


# ---------------------------------------------------------------------------
# CLI wiring — every subsystem needs a wiring test (CLAUDE.md)
# ---------------------------------------------------------------------------


def test_cli_audio_subcommand_is_registered() -> None:
    """``python -m sidequest.cli.validate audio --help`` must succeed —
    the audio validator is reachable through the same dispatch as
    locations + projection-check. Without this, the validator is
    technically present but not invokable from the documented entry
    point."""
    result = subprocess.run(
        [sys.executable, "-m", "sidequest.cli.validate", "audio", "--help"],
        cwd=SERVER_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"`validate audio --help` failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "audio" in result.stdout.lower()


@pytest.mark.parametrize(
    ("fixture_name", "expected_returncode"),
    [
        ("audio_ok", 0),
        ("audio_broken_declared_alias", 1),
    ],
    ids=["clean_pack_exits_zero", "broken_pack_exits_one"],
)
def test_cli_audio_exit_code_matches_result_success(
    fixture_name: str, expected_returncode: int
) -> None:
    """End-to-end exit-code contract: ``ctx.exit(0 if result.success else 1)``
    must hold through the documented CLI entry point. Without this test, a
    regression that hardcoded ``ctx.exit(0)`` would still pass the
    ``--help`` wiring test — CI / ``pf check`` keys off the exit code, so
    the contract has to be proven through subprocess, not just the
    in-process API. Two fixtures: a clean pack (must exit 0) and a pack
    with a declared-broken alias chain (must exit 1)."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sidequest.cli.validate",
            "audio",
            "--genre-packs-root",
            str(FIXTURES / fixture_name),
        ],
        cwd=SERVER_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == expected_returncode, (
        f"fixture {fixture_name!r} must exit {expected_returncode}; "
        f"got returncode={result.returncode} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Robustness — missing audio.yaml + malformed YAML must not crash the walk
# ---------------------------------------------------------------------------


def test_pack_without_audio_yaml_returns_empty_result() -> None:
    """``audio_missing`` has a pack.yaml but no audio.yaml — legitimate
    state for a text-only / stub pack. The validator must return an empty
    ValidationResult (zero errors, zero warnings), NOT crash and NOT emit
    a spurious AUDIO_LOAD_FAILURE. Guards against an inverted-guard
    regression at validate_audio_in_pack's early-return branch."""
    res = _pack_result("audio_missing")
    assert res.errors == [], (
        f"audio-less pack must not produce errors; got {[i.code for i in res.errors]}"
    )
    assert res.warnings == [], (
        f"audio-less pack must not produce warnings; got {[i.code for i in res.warnings]}"
    )
    assert res.success is True


def test_malformed_audio_yaml_records_load_failure_and_does_not_crash() -> None:
    """A syntactically broken audio.yaml must be caught and reported as
    AUDIO_LOAD_FAILURE — never allowed to raise yaml.YAMLError out of the
    per-pack walk. Without this, the audit would crash mid-walk on the
    first content-team-laptop pack that was being edited at validate
    time, defeating the multi-pack robustness guarantee that
    ``test_one_broken_pack_does_not_suppress_others`` claims for the
    pydantic-rejected case but did not actually prove for YAML parse
    failures."""
    res = _pack_result("audio_malformed_yaml")
    failures = [i for i in res.errors if i.code == "AUDIO_LOAD_FAILURE"]
    assert len(failures) == 1, (
        f"expected exactly one AUDIO_LOAD_FAILURE; got errors {[i.code for i in res.errors]}"
    )
    assert "audio.yaml" in failures[0].file


def test_malformed_rules_yaml_records_load_failure_and_does_not_crash() -> None:
    """Symmetric to malformed-audio: a syntactically broken rules.yaml
    must be caught and reported as RULES_LOAD_FAILURE (the dedicated
    code distinguishes the offending file in triage output) rather than
    allowed to crash the walk."""
    res = _pack_result("rules_malformed_yaml")
    failures = [i for i in res.errors if i.code == "RULES_LOAD_FAILURE"]
    assert len(failures) == 1, (
        f"expected exactly one RULES_LOAD_FAILURE; got errors {[i.code for i in res.errors]}"
    )
    assert "rules.yaml" in failures[0].file


def test_validate_packs_does_not_crash_on_any_fixture_root() -> None:
    """The multi-pack walk must complete and accumulate diagnostics from
    every fixture pack even when several are individually broken in
    different ways (declared-alias-cycle, malformed-audio-yaml,
    malformed-rules-yaml, unresolved-rules-mood, audio-missing). No
    exception escapes; sibling-isolation guarantee proven for every
    failure mode we know about."""
    res = validate_packs([FIXTURES])
    # Each broken pack contributes its own error code; ensure we see
    # representatives of all three error families plus the warning.
    error_codes = {(i.code, i.pack) for i in res.errors}
    warning_codes = {(i.code, i.pack) for i in res.warnings}
    assert ("AUDIO_LOAD_FAILURE", "audio_broken_declared_alias") in error_codes
    assert ("AUDIO_LOAD_FAILURE", "audio_malformed_yaml") in error_codes
    assert ("RULES_LOAD_FAILURE", "rules_malformed_yaml") in error_codes
    assert ("UNRESOLVED_RULES_MOOD", "audio_unresolved_rules_mood") in warning_codes
