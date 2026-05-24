"""Narrator prompt sections — loaded from sibling markdown files.

The prose for each prompt section lives in `*.md` files next to this module
so the text can be edited as content (proper diffs, no escape sequences,
editor markdown rendering) without touching `narrator.py`.

Section text is loaded byte-exactly at import time. Do not trim, strip, or
otherwise normalize — the existing test suite asserts exact substrings and
the narrator prompt registry includes the text verbatim in <tags>.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def _load(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


NARRATOR_IDENTITY: str = _load("identity.md")
NARRATOR_CONSTRAINTS: str = _load("constraints.md")
NARRATOR_AGENCY: str = _load("agency.md")
NARRATOR_CONSEQUENCES: str = _load("consequences.md")
NARRATOR_OUTPUT_ONLY: str = _load("output_only.md")
NARRATOR_MAGIC_OUTPUT_RULES: str = _load("magic_output_rules.md")
NARRATOR_OUTPUT_STYLE: str = _load("output_style.md")
NARRATOR_REFERRAL_RULE: str = _load("referral_rule.md")
NARRATOR_COMBAT_RULES: str = _load("combat_rules.md")
NARRATOR_CHASE_RULES: str = _load("chase_rules.md")
NARRATOR_DIALOGUE_RULES: str = _load("dialogue_rules.md")
NARRATOR_POV_RULES: str = _load("pov_rules.md")

__all__ = [
    "NARRATOR_IDENTITY",
    "NARRATOR_CONSTRAINTS",
    "NARRATOR_AGENCY",
    "NARRATOR_CONSEQUENCES",
    "NARRATOR_OUTPUT_ONLY",
    "NARRATOR_MAGIC_OUTPUT_RULES",
    "NARRATOR_OUTPUT_STYLE",
    "NARRATOR_REFERRAL_RULE",
    "NARRATOR_COMBAT_RULES",
    "NARRATOR_CHASE_RULES",
    "NARRATOR_DIALOGUE_RULES",
    "NARRATOR_POV_RULES",
]
