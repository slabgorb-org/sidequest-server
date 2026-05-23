# Intent Router — Mechanical-Engagement Spine

**Date:** 2026-05-22
**Status:** Design — pending implementation plan
**Supersedes in practice:** Story 59-1 (`begin_confrontation` tool) folds into this; Epic 59 reframes around this spine.
**Related ADRs:** ADR-002 (SOUL / Illusionism), ADR-031 (game watcher), ADR-067 (unified narrator), ADR-073 (local fine-tuned router — future backend), ADR-101 (Anthropic SDK backend), ADR-033/093 (confrontation engine), ADR-053 (scenario clue graph), ADR-104/105 (perception firewall).
**Revives:** `sidequest/agents/local_dm.py` (`LocalDM`, dormant since 2026-04-28, commit `74d352c` #96) and `sidequest/agents/subsystems/` (dispatch-bank executor + 3 subsystems).

---

## 1. Problem

SideQuest's mechanical engines are **wired and alive** but **never reliably fire**, because the layer that routes player intent into them is severed.

### 1.1 The engines exist (not the broken part)

Verified inventory of live, reachable engines:

| Engine | Entry point | Mutates |
|--------|-------------|---------|
| Confrontation (all 6 types: combat, chase, negotiation, trial, auction, social_duel, scandal — one `StructuredEncounter` driven by genre `ConfrontationDef`s) | `instantiate_encounter_from_trigger` (`sidequest/server/dispatch/encounter_lifecycle.py:217`) | `snapshot.encounter`, per-actor edge pools |
| Magic / spell working | `apply_magic_working` (`sidequest/server/narration_apply.py:638`); `resolve_magic_confrontation` (`dispatch/confrontation.py:200`) | `snapshot.magic_state.ledger`, `.confrontations` |
| Scenario clue advancement | `consume_clue_footnotes` (`dispatch/scenario_clue_intake.py:34`) | `snapshot.scenario_state.clue_graph`, `KnownFact`s |

### 1.2 The routing is the broken part

Today, every one of those engines fires **only when the narrator self-reports a structured field** — `confrontation=<type>`, `magic_working={...}`, footnotes carrying a `fact_id`. On the default Anthropic-SDK backend (ADR-101) the narrator emits those unreliably or not at all:

- **Story 59-1 (the visible symptom):** in the 2026-05-21 Glenross playtest, an explicit social escalation produced textbook confrontation prose and `confrontation=None` every turn. Root cause (verified): `confrontation` is in `_SDK_TOOL_OWNED_FIELDS` (`orchestrator.py:957`) mapped to the *advance* tools, so the SDK assembler **zeros it** and a fail-loud assertion forces it to stay zero — no tool ever *starts* one. The narrator's emitted field is discarded.
- The component built to read **player** intent and route it into subsystems — `LocalDM` — was **taken off the live path 2026-04-28** (`74d352c`) because, as a second `claude -p` subprocess before the narrator, it doubled per-turn subprocess-spawn latency.

Net: there is **no reliable path from "what the player is trying to do" to "which engine fires."** It all rides on the narrator remembering to emit a sidecar field, which it doesn't. This is the SOUL "Illusionism" failure mode — convincing prose with zero mechanical backing — and it is systemic, not confined to confrontations.

### 1.3 The Zork constraint

SOUL.md's Zork Problem forbids reducing player input to a closed verb set. The fix must **infer** intent without ever gating the open action space: the player can still attempt anything they can articulate; the router only decides *which mechanical engines wake alongside* the narration.

### 1.4 Why now

The 2026-04-28 shelving reason is largely obsolete. The narrator is no longer a subprocess — it's the Anthropic SDK with per-call model routing and prompt caching. A Haiku intent pre-pass is now a cheap API call, not a second subprocess spawn. The `LlmClient` abstraction LocalDM was built with means an ADR-073 local model is a later injection, not a rewrite.

---

## 2. Goal

Restore a single, authoritative routing spine: a pre-narrator pass that reads the player's submitted action, infers intent as **confidence-scored advisory dispatches**, and **engages the matching mechanical engine directly — before the narrator runs.** The narrator then narrates already-real state and cannot wing the mechanics. Every decision emits OTEL so the GM panel can see the engine engaged (not improvised).

**Non-goals:** building new engines (stealth, perception-as-discovery — see §7); replacing the narrator; per-genre intent taxonomies; UI for the router.

---

## 3. Architecture

### 3.1 Components

1. **`IntentRouter`** (rename + revive of `LocalDM`). Stateless per turn. Injected `LlmClient` (Haiku via SDK now; ADR-073 local later). Reads `(player action, state summary)` → emits a `DispatchPackage`:
   - `dispatch[]` — each `{ subsystem, params, confidence (0.0–1.0), depends_on, idempotency_key, visibility }`.
   - `narrator_instructions[]` — `must_narrate` / `must_not_narrate` / `distinctive_detail_for_referent` / `canonical_only_do_not_reveal_to_others`.
   - referent resolution + `confidence_global`.
   - **No `degraded` fallback flag semantics** (see §5 — failure is loud, not a quiet degraded package).

2. **Dispatch vocabulary — live engines only.** The `subsystem` enum is closed to engines that exist:
   - `confrontation` (params: `type`, actors) → `instantiate_encounter_from_trigger`
   - `magic_working` (params: working dict) → `apply_magic_working`
   - `scenario_clue` (params: `fact_id`/clue ref) → `consume_clue_footnotes`
   - `npc_agency` → existing subsystem handler
   - `distinctive_detail_hint` → existing subsystem handler
   - `reflect_absence` → existing subsystem handler

   Excluded with reasons: **stealth** = effect not intent (§7); **perception** = MP info-redaction only, no discovery engine; **gossip** = engine exists but dormant seam (ADR-053), defer; **trope** = automatic per-turn progression, never player-dispatched.

3. **Dispatch-bank executor** (`run_dispatch_bank`, `subsystems/__init__.py:160`) — revived. Topo-sorts by `depends_on`, executes each dispatch through its handler, **engaging engines as a side effect that mutates `snapshot`.** Per-dispatch OTEL.

4. **Lie-detector watcher** — the existing `confrontation_intent_validator` tokenizer, repurposed. *Post-turn*, compares what the router dispatched against what actually engaged on the snapshot; emits a watcher span on mismatch (e.g. router dispatched `confrontation:negotiation` but `snapshot.encounter is None`). Replaces both the deleted keyword scanner and the self-report reprompt. **One mechanism.**

### 3.2 Pipeline

```
player submit
  → IntentRouter.decompose(action, state_summary)        # Haiku via SDK
  → run_dispatch_bank(package)                            # engages engines, mutates snapshot, OTEL per dispatch
  → narrator turn                                         # sees active state + narrator_instructions; narrates consequence
  → narration_apply                                       # engine-owned fields are NO-OP (engines already fired), mirroring today's SDK tool-owned partition
  → lie-detector watcher                                  # classifier-vs-engaged mismatch span
  → broadcast (perception_rewriter applies status-based redaction, incl. stealth effects)
```

### 3.3 Confidence gate

A mechanical dispatch engages its engine **only at or above a confidence threshold** (default proposed: 0.6, tunable per subsystem). Below threshold, the dispatch does **not** fire an engine — it degrades to a `narrator_instruction` hint ("the player may be edging toward a negotiation"). This is the SOUL "untaken bait" guard: never force a confrontation the player didn't commit to. (A below-threshold *non-engagement* is not a fallback — it is the correct, intended decision, and it is logged.)

### 3.4 Retirement (no fallbacks — §5)

The narrator's self-declared engagement fields (`confrontation`, `magic_working` as *engagement triggers*) are **retired**. The SDK assembler stops zeroing-then-discarding `confrontation`; `narration_apply` no longer creates encounters from `result.confrontation`. There is **no fallback to the self-report path.** The router is the sole engagement authority.

---

## 4. Data flow & interfaces

- **Input to router:** `turn_id`, `player_id`, `raw_action`, `state_summary` (compact snapshot encoding — reuse existing slimming), `visibility_baseline`.
- **`DispatchPackage`** (existing pydantic model, `protocol/dispatch.py`) — extend the `subsystem` enum to the §3.2 vocabulary; add `confidence` per dispatch if not already present.
- **Engine handlers** map `subsystem` → existing engine entry point. Each handler is small, single-purpose, independently testable, and emits one OTEL span. Handlers live in `subsystems/` alongside the existing three.
- **Lie-detector** consumes `(package, post_turn_snapshot)` → optional mismatch span. Pure function, no I/O.

---

## 5. Error handling — fail LOUD, no fallbacks

The IntentRouter is a source-of-truth component. **It has no silent fallback.** On failure (Haiku timeout, transport error, unparseable output, schema-invalid package):

- Emit an **ERROR-level** OTEL span (`intent_router.failed`, with reason + raw preview).
- **One bounded, visible retry** (the player sees a "re-reading your action…" beat — explicit, not hidden).
- If retry also fails: **surface the failure to the GM panel and the player as an explicit error.** The turn does **not** silently proceed as narrator-only — that would re-create the exact illusionism this spine exists to kill.

"The table never blocks" is honored by **loud, recoverable** failure, never by quiet degradation. The old `LocalDM` `degraded → empty package → narrator-only` path is **removed**, not ported.

Per-dispatch handler errors are caught per-dispatch (one engine failing doesn't abort the others), logged at WARNING with an OTEL span, and the lie-detector will catch any resulting classifier-vs-engaged mismatch.

---

## 6. Telemetry (OTEL)

Every decision emits a span (the GM panel is the lie detector):

- `intent_router.decompose` — action length, model, confidence_global, dispatch count, latency, retry count.
- `intent_router.dispatch.{subsystem}` — params, confidence, engaged (bool), threshold.
- `intent_router.failed` — ERROR, reason, raw preview (§5).
- `intent_router.lie_detector.mismatch` — dispatched vs engaged divergence.

`local_dm_decompose_span` exists and is reused/renamed.

---

## 7. Stealth & perception (explicitly out — with homes)

- **Stealth is an *effect*, not an intent.** Concealment becomes a status (alongside `invisible`/`blinded`/`deafened`), applied via the existing status path (`apply_status` → `status_changes`). The **`perception_rewriter` at the broadcast layer already redacts peer narration based on those statuses.** The narrator narrates the sneaking; the effect lands as a status; the MP firewall already hides what it should. The router needs no `stealth` dispatch — no stub, nothing reinvented.
- **Perception** stays as MP info-redaction (ADR-104/105). There is no discovery engine; building one would be separate work.

---

## 8. Testing strategy

- **Fixture-based only** (project rule): synthetic genre packs / `ConfrontationDef`s, never live `genre_packs/*`.
- **Router unit tests:** mock `LlmClient`, assert `DispatchPackage` parsing, confidence gating, and the **loud-failure path** (assert ERROR span + no silent narrator-only continuation).
- **Engine-handler tests:** each handler engages its engine on a synthetic snapshot (e.g. `confrontation` dispatch → `snapshot.encounter` created with correct type).
- **Wiring test (mandatory per CLAUDE.md):** drive a synthetic player action through the real pipeline; assert the engine engaged via its OTEL span (not source-text grep).
- **Lie-detector tests:** dispatched-but-not-engaged → mismatch span; engaged-matching → silent (no false positive).
- **Retirement guard:** assert `narration_apply` no longer creates an encounter from `result.confrontation` (the engagement authority moved).

---

## 9. Migration / sequencing

1. Revive `IntentRouter` (Haiku-SDK) + dispatch-bank executor; no engines wired yet (router emits, nothing engages) — internal only, behind no live call.
2. Wire engine handlers one at a time: `confrontation` first (kills 59-1), then `magic_working`, `scenario_clue`, then the three LocalDM subsystems.
3. Insert the router into the live turn pipeline pre-narrator; retire the self-report engagement fields **in the same change** (no parallel period — no two-mechanisms window).
4. Repurpose the validator as the lie-detector watcher.
5. Reframe Epic 59 / fold Story 59-1.

ADR note: this changes the engagement contract — **amend ADR-111** (and reference ADR-067/101/033) in the implementing PR, and write/refresh the LocalDM-revival ADR (the 2026-04-28 offline-only design is reversed for the live path).

---

## 10. Open questions for review

- Confidence threshold default (0.6?) and whether it's per-subsystem from day one.
- Retry count on router failure (1 proposed).
- State-summary encoding for the router: reuse the narrator's slimmed snapshot, or a purpose-built compact view?
- Does the router run per-player on each submitted action under ADR-036 sealed rounds (yes, assumed), and are dispatches merged across players before engine engagement?
