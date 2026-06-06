# 92-2 Local-Rung Enablement Gate — Operator Evidence & Decision

**Date:** 2026-06-06 (operator session, oq-2)
**Decision: NO-GO — the local classification rung stays disabled.**
The 92-2 config seam is merged (server #715) with default `anthropic`;
nothing flips until a GO is recorded against this gate.

## What was run

The 92-1 instrument (server #711) was built but its operator evidence run had
never been executed. This session executed it:

1. **Corpus mined** from real Postgres saves: 295 prompt rows / 39 saves
   (test/probe sessions excluded), real player actions paired with the
   production `_build_state_summary` output, deterministically shuffled
   (seed 922) so the eval's prefix sampling is save/genre-diverse.
   Miner: `artifacts/mine_router_prompts.py`.
2. **Haiku baselines captured** through the production `IntentRouter.decompose`
   (`scripts/router_ab_eval_cli.py --capture`): 295/295 rows, ~5 first-attempt
   schema failures all recovered within the router's bounded retry.
3. **Eval run 1** — qwen2.5:7b-instruct vs live Haiku, N=150 (deterministic
   prefix): `artifacts/router_ab_report_run1.md`.
4. **Eval run 2** (qwen3-coder:30b, same corpus) — **aborted mid-run by
   operator decision**: interim latency p50 13.6s (slower than the 7B; MoE
   prefill did not rescue the fat prompts) plus a new systematic schema
   failure mode (duplicate `idempotency_key` surviving both retry attempts).
   No report written.

## Run 1 verdict (qwen2.5:7b-instruct, unslimmed prompts)

| Gate | Threshold | Actual | |
|------|-----------|--------|---|
| Dispatch-selection agreement | ≥95% | **26.7%** | FAIL |
| qwen schema validity | ≥95% | **86.0%** | FAIL |
| qwen latency p95 | ≤5000ms | **24,134ms** | FAIL |

Worst on the mechanical spine: npc_agency 1/24, scenario_clue 2/22,
confrontation 17/67, movement 15/49. Not adjudication-recoverable.
Systematic qwen schema failure: emitting a forbidden `confidence_global`
field (prompt-coercion fixable, but agreement is the killer).

## Material caveat — prompts have since been slimmed

This eval ran against the **pre-82-10 unslimmed** state summaries
(p50 34.9KB / p95 54.3KB; npcs up to 82% of payload). Story 82-10
(server #716, merged 2026-06-06) cut the router summary to
p50 12.0KB / p95 22.0KB. The fat prompts plausibly drove all three
failures (prompt-eval latency directly; signal-to-noise for agreement and
schema discipline indirectly).

**The fair re-test:** re-mine (`mine_router_prompts.py` now emits slimmed
summaries), re-capture (~$1.50 of Haiku), re-run both models. ~30 min
end-to-end. Until that re-test records a GO, the doctrine holds: no flip.

## Corpus regeneration

The 10MB corpora (`router_prompts.jsonl`, `router_corpus.jsonl`) are
gitignored — re-derivable from the saves DB:

```bash
SIDEQUEST_DATABASE_URL=... SIDEQUEST_GENRE_PACKS=... \
  uv run python artifacts/mine_router_prompts.py --out artifacts/router_prompts.jsonl
uv run python scripts/router_ab_eval_cli.py --capture \
  --prompts-jsonl artifacts/router_prompts.jsonl --out artifacts/router_corpus.jsonl
```

## Methodology caveats

- Only the FINAL snapshot per save is persisted (ADR-115), so every action in
  a save is paired with that save's final-state summary rather than the
  round-accurate one. Both backends see byte-identical inputs, so the
  agreement metric is unaffected by the time shift.
- The run-twice nondeterminism characterization was not performed: run 1's
  verdict was too lopsided for spread to matter (26.7% cannot bridge to 95%).
  Required before any future GO is recorded.
- Live Haiku itself showed a measurable first-attempt schema-failure rate
  during capture (~5/295) — the schema-validity comparison baseline is not
  perfection.
