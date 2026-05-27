# Bind `space_opera` to `ruleset: swn` — Faithful Attack-vs-AC Combat

**Date:** 2026-05-26
**Status:** Design approved
**Epic:** SWN RulesetModule (faithful Stars Without Number)
**Predecessors:** `2026-05-26-pluggable-srd-ruleset-modules-design.md`, `2026-05-26-swn-module-design.md`
**Plan ref:** `2026-05-26-swn-module-attack-and-checks.md` (P1–P3 landed, PR #468)
**Realizes:** the deferred **P6** (space_opera content re-author to SWN) plus a stat-bridge mechanism.

## Problem

`SwnRulesetModule` (attack d20-vs-AC, 2d6 skill checks, d20 saves) is built, registered, and
reachable from the `dispatch_check` and `dispatch_dice_throw` resolution paths — but **no pack binds
it**, so it has never fired in real play. Binding `space_opera` exposes two obstacles:

1. **The opposed_check bypass.** space_opera's `combat` (Firefight) and `ship_combat` declare
   `resolution_mode: opposed_check`. That path (`narration_apply._opposed_dc`,
   `opposed_check.py:86`) computes DC with hand-copied native math and never consults the ruleset
   module — so binding SWN would *not* change combat. Worse, opposed d20-vs-d20 is not the shape of
   SWN combat at all: SWN resolves **d20 + attack-bonus vs static target AC**. See
   memory `project_opposed_check_wiring_trap`.

2. **The stat-vocabulary mismatch (saves only).** Attack beats and skill checks already speak the
   pack's **flavor** stat names — Firefight beats declare `stat_check: Physique`/`Resolve`/`Reflex`,
   and skill-check `attribute` arrives from the payload as a flavor stat — so those modifiers already
   compute correctly via the SWN curve against the player's (flavor-keyed) stat dict. **Saves are the
   gap:** the module's `_SAVE_ATTRS` hardcodes SWN attribute names (physical=STRENGTH+CONSTITUTION,
   evasion=DEXTERITY+INTELLIGENCE, mental=WISDOM+CHARISMA), which are **not keys** in a flavor-stat
   dict — so `_stat` silently falls back to a neutral 10 (→ modifier 0) and every save modifier is a
   dead 0. space_opera ships **six** flavor stats (Physique, Reflex, Intellect, Cunning, Resolve,
   Influence), a clean 1:1 bijection to SWN's six attributes, so a per-pack `attribute_map` lets the
   save logic translate its hardcoded SWN attribute names to the pack's flavor stats. The silent
   neutral-10 fallback also violates the project's "no silent fallback" principle and is removed.

## Decisions (locked during brainstorm)

| Decision | Choice |
|----------|--------|
| Pack & worlds | `space_opera`, both worlds (`aureate_span`, `coyote_star`) — one pack-level line |
| Combat resolution | Switch **both** `combat` (Firefight) and `ship_combat` from `opposed_check` → `beat_selection` (attack-vs-AC) |
| Win condition | **Drop the momentum / engagement_range dials for SWN combat.** Combat resolves on **HP depletion** (0 HP → defeat), not a dial threshold. Both `combat` and `ship_combat` use it. |
| Content depth | Faithful slice: author `attack_bonus`/`combat_skill` on beats, `armor_class` (and hull-as-HP for ships) on opponents |
| Stat bridge | `attribute_map` in `RulesConfig.swn`, translating at the module seam — **no chargen change** |
| Legacy numbers | Discard freely. Combat was RP-only and never mechanically resolved; no calibration debt to honor. |

Out of scope: psionics (P7), the per-module narrator tool contract (P5), and the **full SWN ship
subsystem** (mass/power/hardpoints, crew action economy, ship classes). `ship_combat` adopts the same
attack-vs-AC + HP-as-win-condition shape as personal combat — the enemy ship is a combatant core with
a ship Armor Class and hull-as-HP. A dedicated SWN ship-stat subsystem is a separate future effort.

### Doctrine divergence (recorded, not silent)

The ablative-HP design (`2026-05-25-swn-crunch-ablative-hp-design.md`, the playgroup mandate) places
HP **under** the dials — "dials sit on top of HP," "HP additive under the dials" (§1.2). This binding
**diverges for SWN combat**: HP becomes the win condition and the dial is dropped. This is authorized
by Keith's live instruction (which outranks the spec per spec-authority) and is scoped narrowly — the
dial-on-top doctrine **remains the default** for every other pack and for SWN *non-combat*
confrontations (negotiation keeps its `leverage` dial; dogfight keeps sealed-letter). The mechanism is
a per-confrontation `win_condition` selector, so this is a *refinement* (make it configurable) rather
than a global reversal.

## Architecture

Reuse-first. The work is **wiring real play to reach an existing module** and **authoring the
content it reads** — not new subsystems. Already present and reused unchanged: `SwnRulesetModule`,
`BeatDef.attack_bonus`/`combat_skill` (declared, default 0), `CreatureCore.armor_class` (declared,
default 10), the `dispatch_dice_throw` single-roll attack path that calls `ruleset.attack_params`,
and the `damage_resolver` (ADR-114).

Two engine changes are required: the **stat-bridge** (also fixes a silent-fallback violation) and the
**`win_condition` selector** with an HP-depletion resolution branch. Both are small, single-seam
additions that default to today's behavior for every other pack.

### The win condition

The encounter engine resolves only on dial-threshold, composure-break, or an explicit resolution beat
(`beat_kinds.py` ~824–835) — there is **no HP-depletion branch**, and `player_metric`/`opponent_metric`
are required by the model. Add a `win_condition` enum to `ConfrontationDef`:

- `dial_threshold` (**default** — every existing pack and confrontation unchanged)
- `hp_depletion` (SWN `combat` + `ship_combat`)

When `hp_depletion`: a new branch in `apply_beat`, evaluated **before** the dial checks, resolves the
encounter the moment a side's primary combatant reaches 0 HP — `player_victory` if an opposing-side
combatant drops, `opponent_victory` if a player-side combatant drops. The dial-threshold branches are
**skipped** for that confrontation, and `player_metric`/`opponent_metric` become optional (validator:
required only when `win_condition == dial_threshold`). Resolution gates on `win_condition` at **one
seam** — no scattered null-guards. Composure-break and resolution-beat branches stay orthogonal.

HP is already tracked: strike damage lands on the target `CreatureCore` via `apply_beat_hp_channel`
(`beat_kinds.py`). The win-condition branch only adds the *resolution* check, reusing that HP state.

### The stat bridge

Add `attribute_map: dict[str, str]` to `SwnConfig` — SWN attribute name → pack flavor-stat:

```yaml
rules:
  ruleset: swn
  swn:
    attribute_map:
      STRENGTH:     Physique
      CONSTITUTION: Resolve
      DEXTERITY:    Reflex
      INTELLIGENCE: Intellect
      WISDOM:       Cunning
      CHARISMA:     Influence
```

Save pairs then resolve through flavor-stats:

- physical (STR+CON) → `max(Physique, Resolve)`
- evasion (DEX+INT) → `max(Reflex, Intellect)`
- mental (WIS+CHA) → `max(Cunning, Influence)`

Only `save_params` consults the map: it translates each `_SAVE_ATTRS` SWN attribute name to its
flavor stat via `cfg.attribute_map`, then scores via `_stat`. `attack_params` and `check_params` are
**unchanged** — they already operate on flavor stat names from content/payload, so they need neither
`cfg` nor the map. `_stat`'s silent neutral-10 fallback is replaced with fail-loud (a looked-up stat
absent from the dict is a content/map bug, not a default).

## Changes by repo

### server (`sidequest-server`)

- **`genre/models/rules.py`** — add `attribute_map: dict[str, str]` to `SwnConfig`. Validation lives
  on **`RulesConfig`** (a `mode="after"` validator), which carries `ruleset`, `swn`, **and**
  `ability_score_names` (rules.py:648) — so when `ruleset == "swn"` it can check in one place: all six
  SWN attribute keys present (STRENGTH/CONSTITUTION/DEXTERITY/INTELLIGENCE/WISDOM/CHARISMA), the map
  non-empty, **and** every mapped value ∈ `ability_score_names`. Fail loud on missing key or unknown
  stat — **no auto-default map, no silent fallback.** (The existing `_populate_swn_defaults` validator
  on `RulesConfig` is the sibling to extend or sit beside.)
- **`game/ruleset/swn.py`** — only `save_params` changes: translate each `_SAVE_ATTRS` SWN attribute
  name to its flavor stat via `cfg.attribute_map`, then score. Replace `_stat`'s neutral-10 silent
  fallback with fail-loud (a stat absent from the dict is a loud error — the validator should make it
  unreachable, but the module must not paper over it). `attack_params`/`check_params` unchanged.
- **`genre/models/rules.py` (ConfrontationDef)** — add `win_condition: WinCondition = dial_threshold`
  (new `WinCondition` StrEnum: `dial_threshold` | `hp_depletion`). Make `player_metric`/`opponent_metric`
  optional (`MetricDef | None = None`); the after-validator requires them only when
  `win_condition == dial_threshold`. Default preserves every existing pack.
- **`game/encounter.py` (StructuredEncounter)** — add `win_condition: WinCondition = dial_threshold`
  so the runtime encounter (read by `apply_beat`) knows its resolution rule. Stamped at init.
- **`game/beat_kinds.py`** — add the `hp_depletion` resolution branch (evaluated *before* the dial
  checks): resolve when the primary opposing/player combatant core's `hp.current <= 0` (cores via the
  existing `edge_resolver`). Guard the dial-threshold branches (`beat_kinds.py:824-830`) behind
  `enc.win_condition == dial_threshold`. Single gating seam.
- **Init seam, not a reader audit.** The live `StructuredEncounter` is built from the cdef metrics at
  `dispatch/encounter_lifecycle.py:460-474`, and ~9 downstream readers (CONFRONTATION payload, narrator
  framing, `query_encounter`/`advance_confrontation` tools, morale logic) assume **non-None live
  metrics**. So we do **not** make the live metrics optional — instead, at the init seam, when a combat
  omits metrics, **synthesize inert `EncounterMetric`s** (name `"hp"`, `current=0`, high `threshold`
  that the gated dial branch never checks). That keeps every existing reader safe with one localized
  change. Stamp `enc.win_condition` here from the cdef.
- **`dispatch/confrontation.py` (`build_confrontation_payload`, ~line 186)** — include `win_condition`
  in the CONFRONTATION frame, and for `hp_depletion` surface the combatants' HP (primary player +
  opponent core `hp.current`/`hp.max`) so the UI can render an HP track instead of the inert dial.
  (UI rendering of the HP track is a `sidequest-ui` follow-up; the server delivers the frame data.)
- **No change to `dispatch_dice_throw` control flow.** Flipping the confrontation off `opposed_check`
  routes it through the existing single-roll branch where `attack.target_number` is the opponent's
  AC. The opposed branch (`narration_apply`) is simply no longer the combat path for this pack.

### content (`sidequest-content`)

- **`space_opera/rules.yaml`**
  - `rules.ruleset: swn` + `rules.swn.attribute_map` (above).
  - `combat` (Firefight) and `ship_combat`: `resolution_mode: opposed_check` → `beat_selection`, and
    `win_condition: hp_depletion`.
  - **Remove `player_metric` / `opponent_metric`** from both combats — the momentum / engagement_range
    dials are dropped. HP is now the visible track (ADR-040, as amended by the ablative-HP design).
  - Author `attack_bonus` / `combat_skill` on strike-kind beats; calibrate fresh.
  - `opponent_default_stats` retained (now read through the map); values recalibrated as needed.
  - Negotiation keeps its `leverage` dial (`win_condition: dial_threshold`, unchanged); `dogfight`
    keeps `sealed_letter_lookup`.
- **Opponent creatures** — author `armor_class` **and HP** so the materialized `CreatureCore` carries
  real AC and a real HP pool (the win condition). Confirm the materialization seam
  (`world_materialization._apply_npc` / creature seeding) populates `CreatureCore.armor_class` and HP
  from content; `creature_core.py:118` documents the AC intent — verify it is actually wired, not just
  commented.
- **Enemy ships** (`ship_combat`) — represented as a combatant core with a ship **Armor Class** and
  **hull-as-HP**. Ship attacks resolve d20 + attack-mod vs ship AC; damage ablates hull; the encounter
  resolves at 0 hull. Identical resolution path to personal combat — no separate ship subsystem.

## Data flow — combat turn (post-switch)

1. Player selects a strike beat (e.g. `shoot`, `broadside`).
2. Engine rolls **d20 + `attack.modifier`** where
   `modifier = attack_bonus + combat_skill + attribute-mod` (the attribute mod is the SWN curve over
   the beat's flavor `stat_check`, e.g. Physique — no map needed on the attack path),
   against **`target_number = opponent.armor_class`**.
3. Outcome tier derived from the single roll vs AC.
4. `apply_beat` fires `damage_resolver` → weapon dice → ablates opponent **HP** via
   `apply_beat_hp_channel`. No dial deltas — the momentum / engagement_range metrics are gone.
5. The `hp_depletion` resolution branch (evaluated before the now-skipped dial checks) ends the
   encounter the instant a side's primary combatant reaches **0 HP** — `player_victory` /
   `opponent_victory`. **HP is the win condition**, not a track beneath a dial.
6. Opponent's beat tier stays narrator-fiat — the `beat_selection` contract (no opposing roll).
7. `ship_combat` runs the identical loop with ship AC and hull-as-HP.

## Saves & skill checks

Already wired via `dispatch_check` (CHECK_THROW). Skill checks already resolve (payload `attribute` is
a flavor stat). **Saves** are what `attribute_map` fixes — once it lands, `save_params` translates its
SWN save-attribute pairs to flavor stats and the previously-dead-zero save modifier becomes real.

## Error handling

- Pack load fails loud on missing/incomplete `attribute_map` or a mapping to a non-existent stat.
- `compute_dc` stays `NotImplementedError` for SWN (attacks resolve via `attack_params`).
- No degenerate neutral-10 stat fallback survives in the module.

## OTEL

- `encounter.beat_applied` already emits modifier / target / tier. **Verify** the attack resolution
  logs `target_number` as the opponent AC (not a static DC) so the GM panel can distinguish SWN math
  from native.
- Add an attribute-map resolution trace on the **save** path when a SWN save-attribute is translated —
  the lie-detector that the bridge is engaged rather than scoring a silent-0 save.
- Emit a span on the `hp_depletion` resolution branch (which side dropped, at which beat, final HP) so
  the GM panel can confirm combat ended on HP — not on a dial or narrator fiat.

## Testing

- **Module unit:** `save_params` resolves all three save pairs through `attribute_map` (e.g. physical
  reads `max(Physique, Resolve)`), producing a non-zero modifier where the old hardcoded-SWN path gave
  0; and `attack_params` still computes the attribute mod from the beat's flavor `stat_check` with no
  map. Confirm `_stat` now raises on an absent stat instead of returning 10.
- **Validator:** missing key, incomplete map, and map-to-unknown-stat each fail loud at load.
  Also: `win_condition: dial_threshold` without metrics fails loud; `hp_depletion` without metrics
  loads clean.
- **Win-condition unit:** `apply_beat` under `hp_depletion` resolves on 0 HP (player & opponent
  sides) and does **not** resolve on dial threshold; under `dial_threshold` behavior is unchanged.
- **Wiring test (mandatory).** Drive a real Firefight beat in `space_opera` through
  `dispatch_dice_throw` end-to-end: assert the d20 resolves vs the **authored opponent AC**, a hit
  ablates **HP**, and the encounter resolves when HP hits 0 (assert via the resolution OTEL span, not
  source-text). Per `project_opposed_check_wiring_trap`, run against the **real pack**, not a synthetic
  non-opposed fixture; additionally assert the opposed branch is no longer reached for `space_opera`
  combat. Add the same end-to-end assertion for `ship_combat` (hull-as-HP).
- **Load both worlds clean** (`aureate_span`, `coyote_star`) under `ruleset: swn`.

## Sequencing

One spec, ~3 plan phases:

1. **Engine changes** — (a) `SwnConfig.attribute_map` + `RulesConfig` validator, `save_params`
   translation, `_stat` silent-fallback removal; (b) `ConfrontationDef.win_condition` enum, optional
   metrics + validator, `StructuredEncounter.win_condition`, the init-seam inert-metric synthesis, the
   `hp_depletion` resolution branch, dial-branch gating, the resolution OTEL span, and `win_condition`
   + HP in the CONFRONTATION payload. Unit + validator tests for both.
2. **Binding + mode-switch + content** — `rules.yaml` ruleset/map, `resolution_mode` →
   `beat_selection`, `win_condition: hp_depletion`, metric removal on both combats, beat
   `attack_bonus`/`combat_skill`, opponent `armor_class` + HP, enemy-ship AC + hull, materialization
   verification, the end-to-end wiring tests (personal + ship), both-worlds load test.
3. **Calibration pass** — tune attack bonuses, AC, HP/hull pools, and weapon dice for playable SWN
   feel (free hand; no legacy numbers).
