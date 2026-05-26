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

2. **The stat-vocabulary mismatch.** SWN's attribute modifier and save-pair logic key on the six SWN
   attribute names (STRENGTH/DEXTERITY/CONSTITUTION/INTELLIGENCE/WISDOM/CHARISMA). space_opera ships
   **five** flavor stats (Physique, Reflex, Intellect, Cunning, Resolve). The module's `_stat`
   helper silently falls back to a neutral 10 (→ modifier 0) for any unrecognized stat — so the
   attribute contribution to every attack, check, and save would be a dead 0. This silent fallback
   also violates the project's "no silent fallback" principle.

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
      CONSTITUTION: Physique
      DEXTERITY:    Reflex
      INTELLIGENCE: Intellect
      WISDOM:       Resolve
      CHARISMA:     Cunning
```

Save pairs then resolve through flavor-stats:

- physical (STR+CON) → `max(Physique, Physique)` = Physique
- evasion (DEX+INT) → `max(Reflex, Intellect)`
- mental (WIS+CHA) → `max(Resolve, Cunning)`

The module's `_stat` / `stat_modifier` / `_SAVE_ATTRS` consult `cfg.attribute_map` to resolve the
flavor-stat before scoring. `attack_params` gains `cfg` (already threaded into `check_params` /
`save_params`).

## Changes by repo

### server (`sidequest-server`)

- **`genre/models/rules.py`** — add `attribute_map` to `SwnConfig`. Model-validator: when
  `ruleset == "swn"`, require all six SWN attribute keys present **and** every mapped value to be a
  member of the pack's declared stat list. Fail loud on missing key or unknown stat — **no silent
  fallback, no auto-default map.**
- **`game/ruleset/swn.py`** — `_stat`, `stat_modifier`, and the `_SAVE_ATTRS` save logic resolve
  through `cfg.attribute_map`. Remove the neutral-10 silent fallback; an unmapped stat is a loud
  error (the validator should make it unreachable, but the module must not paper over it).
  `attack_params` signature gains `cfg`.
- **`game/models/rules.py` (ConfrontationDef)** — add `win_condition: WinCondition = dial_threshold`.
  Make `player_metric`/`opponent_metric` optional (`MetricDef | None`); validator requires them only
  when `win_condition == dial_threshold`. Default preserves every existing pack.
- **`game/beat_kinds.py`** — add the `hp_depletion` resolution branch (before the dial checks); guard
  the dial-threshold branches behind `win_condition == dial_threshold`. Single gating seam.
- **Reader audit** — consumers that read `enc.player_metric`/`opponent_metric` unconditionally (UI
  broadcast, snapshot encoding, narration framing) must tolerate `None` under `hp_depletion`. Audit
  and guard at the encoding seam, not per call-site (avoid burying bombs). This is explicit plan work.
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
   `modifier = attack_bonus + combat_skill + mapped-attribute-mod`,
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

Already wired via `dispatch_check` (CHECK_THROW). After `attribute_map` lands, no further wiring —
2d6 skill checks and d20 saves resolve through the mapped flavor-stats automatically.

## Error handling

- Pack load fails loud on missing/incomplete `attribute_map` or a mapping to a non-existent stat.
- `compute_dc` stays `NotImplementedError` for SWN (attacks resolve via `attack_params`).
- No degenerate neutral-10 stat fallback survives in the module.

## OTEL

- `encounter.beat_applied` already emits modifier / target / tier. **Verify** the attack resolution
  logs `target_number` as the opponent AC (not a static DC) so the GM panel can distinguish SWN math
  from native.
- Add an attribute-map resolution trace when a mapped stat is consulted — the lie-detector that the
  bridge is actually engaged rather than scoring a silent 0.
- Emit a span on the `hp_depletion` resolution branch (which side dropped, at which beat, final HP) so
  the GM panel can confirm combat ended on HP — not on a dial or narrator fiat.

## Testing

- **Module unit:** `attribute_map` resolution for attack modifier and all three save pairs;
  confirm `stat_modifier` reads the flavor-stat, not the SWN name.
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

1. **Engine changes** — (a) `SwnConfig.attribute_map`, module resolution, validator,
   silent-fallback removal; (b) `ConfrontationDef.win_condition` enum, optional metrics + validator,
   the `hp_depletion` resolution branch, dial-branch gating, the reader audit at the encoding seam,
   and the resolution OTEL span. Unit + validator tests for both.
2. **Binding + mode-switch + content** — `rules.yaml` ruleset/map, `resolution_mode` →
   `beat_selection`, `win_condition: hp_depletion`, metric removal on both combats, beat
   `attack_bonus`/`combat_skill`, opponent `armor_class` + HP, enemy-ship AC + hull, materialization
   verification, the end-to-end wiring tests (personal + ship), both-worlds load test.
3. **Calibration pass** — tune attack bonuses, AC, HP/hull pools, and weapon dice for playable SWN
   feel (free hand; no legacy numbers).
