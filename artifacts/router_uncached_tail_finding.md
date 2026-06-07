# COST Finding — Intent Router uncached `user`-prompt tail

**Date:** 2026-06-06
**Source:** live playtest forensics (sq-llm-costs), oq-1 server on :8765
**Severity:** LOW (optimization, not an incident — the big router wins already landed)
**Root cause class:** code (prompt structure) + design (caching boundary)

## Summary

Post-91-2 (8x→1x) and 91-3 (cache-floor guard), the Intent Router's per-turn
Haiku cost is dominated by the **uncached `user` message**, which carries the
per-turn state summary and is re-billed in full every turn. The cacheable
tools+system prefix already reads back warm; the `user` block has **no
`cache_control` marker** and cannot, because it mutates each turn.

This is the only Haiku cost lever left: the local-Qwen rung that would have
moved this work off the Anthropic bill is **NO-GO** (92-2 gate: qwen2.5:7b 86%
schema vs ≥95%, p95 24,134ms vs ≤5000ms).

## Evidence

Live router usage lines (rotated `sidequest-server.log.20260606-035243`):

```
caller=intent_router  input=3019  cache_read=4406  cache_write=0  cost=$0.0050
caller=intent_router  input=3082  cache_read=4406  cache_write=0  cost=$0.0045
caller=intent_router  input=3204  cache_read=4406  cache_write=0  cost=$0.0054
... 7 calls, all ~$0.005, cache_read pinned at 4406
```

- **Cached prefix (4,406 read):** system prompt (~2,760 tok) + DispatchPackage
  tool schema (~1,970 tok) ≈ 4,730 combined, 1h ephemeral, cleared the floor.
  `llm_factory._IntentRouterLlm.emit_tool` — single `cache_control` on the
  system block (canonical order tools→system→messages).
- **Uncached tail (~3,100):** `intent_router._build_user_prompt` →
  `<game_state>{slimmed_summary}</game_state><raw_action>{action}</raw_action>`.
  No marker. The `action` is tiny; the bulk is the serialized state summary.

State-summary slimming (82-10) **is** working — `prompt.game_state.bytes`
shows `bytes_before≈35,900 → bytes_after≈18,000–21,000 (ratio 0.51–0.59)`.
The tail is already roughly halved from raw; what remains is the irreducible
per-turn game state plus a quasi-static remnant.

## Cost framing (honest)

- Router ≈ **$0.005/turn** today. At ~84 turns/day ≈ **$0.42/day**.
- The 06-05 ~$11 Haiku day was the pre-91-2 8x-uncached pathology, **already
  fixed**. The router is no longer a significant spender.
- A tail cut (below) roughly halves the uncached portion → ~$0.002–0.003/turn,
  saving **~$0.20–0.25/day**. Marginal. File as tracked optimization, not urgent.

## Recommended lever (ADR-110 diff-with-anchor, deferred)

Split the `user` content into a **stable sub-block + volatile delta** and put a
second `cache_control` marker on the stable sub-block:

- Quasi-static within a session (rarely changes turn-to-turn): character sheets,
  `witnessed_act_vocabulary`, `confrontation_types`, current-room description.
- Truly volatile (changes every turn): positions, HP, recent events, the
  `raw_action`.

Anchor-caching the stable sub-block would drop the uncached tail from ~3,100 to
the ~1,000–1,500-token volatile delta. This is precisely the ADR-110
"diff-with-anchor" work currently deferred for the narrator — the router is a
smaller, simpler place to prove it.

**Caveat (No Silent Fallbacks):** any second cache block must clear Haiku's
4,096-token floor *on its own*, or it silently never caches — the exact 91-3
trap. If the stable sub-block is sub-floor, do NOT add a marker; the win isn't
there. Re-measure with `count_tokens` (the opt-in
`test_intent_router_prefix_token_floor_live` pattern) before shipping.

## Decision needed

LOW priority. Worth doing only if ADR-110 diff-with-anchor is picked up anyway —
then the router is the cheap first target. Standalone, the ~$0.25/day saving
does not justify the complexity. Recommend: **park behind ADR-110**, do not
schedule independently.
