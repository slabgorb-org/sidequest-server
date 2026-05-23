"""Source-grep guard: no production code path may set classified_intent='unknown'.

Spec 2026-05-20 confrontation-intent-validator step 6. The literal "unknown"
was a stub from before ActionRewrite.intent was wired. Real values:
- action_rewrite.intent verbatim (happy path)
- matched_type (validator-dispatch mismatch path)
- 'unspecified' (intent omitted by narrator)

The grep is narrow: sidequest/ production source only; tests/ excluded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2] / "sidequest"


def test_no_classified_intent_unknown_in_production_source() -> None:
    """No production code path may write `classified_intent="unknown"`."""
    result = subprocess.run(
        ["grep", "-rn", '"unknown"', str(SERVER_ROOT)],
        capture_output=True,
        text=True,
    )
    offenders = [line for line in result.stdout.splitlines() if "classified_intent" in line]
    assert offenders == [], (
        f"Production code still hardcodes classified_intent='unknown': {offenders}"
    )
