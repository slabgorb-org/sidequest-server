"""Spec 2026-05-20: one mechanism. The legacy prose-regex scanner is dead.

Source-grep guard so the deleted symbols can't sneak back in via revert
or copy-paste. Pinned alongside the existing
test_intent_classified_invariant.py guard.

NOTE: This guard targets the SCANNER symbols in narration_apply.py only.
It deliberately does NOT forbid the prompt-guardrail constant
CONFRONTATION_TRIGGER_CONSTRAINT in narrator_guardrails.py — that's a
different mechanism (prompt steering, not prose regex) and is kept per
spec. The string 'confrontation_trigger_constraint' (lowercase, used as
a prompt-section name) is similarly KEPT and NOT in the forbid list.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2] / "sidequest"


def test_no_confrontation_trigger_patterns_constant_in_source() -> None:
    result = subprocess.run(
        ["grep", "-rn", "_CONFRONTATION_TRIGGER_PATTERNS", str(SERVER_ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.stdout == "", (
        f"Legacy scanner symbol resurrected: {result.stdout}"
    )


def test_no_scan_for_confrontation_trigger_keywords_in_source() -> None:
    result = subprocess.run(
        ["grep", "-rn", "_scan_for_confrontation_trigger_keywords", str(SERVER_ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.stdout == "", (
        f"Legacy scanner function resurrected: {result.stdout}"
    )


def test_no_skipped_with_trigger_keywords_watcher_event() -> None:
    """The old state_transition op the scanner emitted is also dead."""
    result = subprocess.run(
        ["grep", "-rn", "skipped_with_trigger_keywords", str(SERVER_ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.stdout == "", (
        f"Legacy scanner watcher event resurrected: {result.stdout}"
    )
