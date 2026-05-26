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
| Content depth | Faithful slice: author `attack_bonus`/`combat_skill` on beats, `armor_class` on opponents |
| Stat bridge | `attribute_map` in `RulesConfig.swn`, translating at the module seam — **no chargen change** |
| Legacy numbers | Discard freely. Combat was RP-only and never mechanically resolved; no calibration debt to honor. |

Out of scope: psionics (P7), the per-module narrator tool contract (P5), `ship_combat` as a distinct
SWN vehicle subsystem (it adopts the same attack-vs-AC path for now).

## Architecture

Reuse-first. The work is **wiring real play to reach an existing module** and **authoring the
content it reads** — not new subsystems. Already present and reused unchanged: `SwnRulesetModule`,
`BeatDef.attack_bonus`/`combat_skill` (declared, default 0), `CreatureCore.armor_class` (declared,
default 10), the `dispatch_dice_throw` single-roll attack path that calls `ruleset.attack_params`,
and the `damage_resolver` (ADR-114).

One engine change is unavoidable — the stat-bridge — and it doubles as fixing the silent-fallback
violation.

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
- **No change to `dispatch_dice_throw` control flow.** Flipping the confrontation off `opposed_check`
  routes it through the existing single-roll branch where `attack.target_number` is the opponent's
  AC. The opposed branch (`narration_apply`) is simply no longer the combat path for this pack.

### content (`sidequest-content`)

- **`space_opera/rules.yaml`**
  - `rules.ruleset: swn` + `rules.swn.attribute_map` (above).
  - `combat` (Firefight) and `ship_combat`: `resolution_mode: opposed_check` → `beat_selection`.
  - Author `attack_bonus` / `combat_skill` on strike-kind beats; calibrate fresh.
  - `opponent_default_stats` retained (now read through the map); values recalibrated as needed.
- **Opponent creatures** — author `armor_class` so the materialized `CreatureCore` carries real AC.
  Confirm the materialization seam (`world_materialization._apply_npc` / creature seeding) populates
  `CreatureCore.armor_class` from content armor; `creature_core.py:118` documents this intent —
  verify it is actually wired, not just commented.

## Data flow — combat turn (post-switch)

1. Player selects a strike beat (e.g. `shoot`, `broadside`).
2. Engine rolls **d20 + `attack.modifier`** where
   `modifier = attack_bonus + combat_skill + mapped-attribute-mod`,
   against **`target_number = opponent.armor_class`**.
3. Outcome tier derived from the single roll vs AC.
4. `apply_beat` applies the **momentum / engagement_range dial deltas** *and* fires `damage_resolver`
   → weapon dice → opponent **HP**.
5. Dials remain the confrontation win condition (threshold 7); **HP is the ablative lethality track
   beneath the dials** (ADR-114), not a parallel win condition.
6. Opponent's beat tier stays narrator-fiat — the `beat_selection` contract (no opposing roll).

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

## Testing

- **Module unit:** `attribute_map` resolution for attack modifier and all three save pairs;
  confirm `stat_modifier` reads the flavor-stat, not the SWN name.
- **Validator:** missing key, incomplete map, and map-to-unknown-stat each fail loud at load.
- **Wiring test (mandatory).** Drive a real Firefight beat in `space_opera` through
  `dispatch_dice_throw` end-to-end: assert the d20 resolves vs the **authored opponent AC** and that
  a hit lands damage on **HP**. Per `project_opposed_check_wiring_trap`, run against the **real pack**,
  not a synthetic non-opposed fixture; additionally assert the opposed branch is no longer reached
  for `space_opera` combat.
- **Load both worlds clean** (`aureate_span`, `coyote_star`) under `ruleset: swn`.

## Sequencing

One spec, ~2–3 plan phases:

1. **Stat-bridge engine change** — `SwnConfig.attribute_map`, module resolution, validator,
   silent-fallback removal, unit + validator tests.
2. **Binding + mode-switch + content** — `rules.yaml` ruleset/map/resolution-mode, beat
   `attack_bonus`/`combat_skill`, opponent `armor_class`, materialization verification, the
   end-to-end wiring test, both-worlds load test.
3. **Calibration pass** — tune attack bonuses, AC values, and weapon dice for playable SWN feel
   (free hand; no legacy numbers).
