# Narrator Prompts — Stub Audit

Audit log for the `narrator_prompts/*.md` directory. Append new entries with date
and finding. Sized for token-budget work (Epic 57): a silently-empty section is a
free win, so the question gets re-asked periodically.

## 2026-05-19 — Story 57-2

**Question:** Are any of the `*.md` prompt sections empty stubs (load-bearing but
zero-content), silently contributing nothing to the assembled narrator prompt?

**Finding:** No. All 11 sections are substantive and integrated.

| File | Bytes | Constant |
|------|------:|----------|
| identity.md         |    210 | `NARRATOR_IDENTITY`         |
| constraints.md      |    792 | `NARRATOR_CONSTRAINTS`      |
| agency.md           |  1,269 | `NARRATOR_AGENCY`           |
| consequences.md     |    398 | `NARRATOR_CONSEQUENCES`     |
| output_only.md      | 24,698 | `NARRATOR_OUTPUT_ONLY`      |
| output_only_sdk.md  | 23,475 | `NARRATOR_OUTPUT_ONLY_SDK`  |
| output_style.md     |    667 | `NARRATOR_OUTPUT_STYLE`     |
| referral_rule.md    |    324 | `NARRATOR_REFERRAL_RULE`    |
| combat_rules.md     |  1,825 | `NARRATOR_COMBAT_RULES`     |
| chase_rules.md      |    957 | `NARRATOR_CHASE_RULES`      |
| dialogue_rules.md   |    756 | `NARRATOR_DIALOGUE_RULES`   |

**Integration chain (verified):**

1. `__init__.py` loads each file via `_load()` and exports a `NARRATOR_*` constant.
2. `narrator.py` re-imports all 11 constants and re-exports via `__all__`.
3. `orchestrator.py` registers sections into `PromptRegistry` via
   `register_section(…)` call sites.

No orphaned `.md` files in the directory. No `NARRATOR_*` constants reference
files that do not exist. No empty placeholder files.

**Origin of the audit concern:** PR #252 (2026-05-11, refactor/narrator) first
extracted these sections from inline strings into `.md` files. Some sections may
have been minimal at extraction time. Since then, content has been backfilled.

**Conclusion:** No silent-empty token savings available in this directory. The
larger token-reduction wins for Epic 57 live in stories 57-3 (cache promotion),
57-4 (guardrail → tool descriptions), and 57-5 (game_state slimming).
