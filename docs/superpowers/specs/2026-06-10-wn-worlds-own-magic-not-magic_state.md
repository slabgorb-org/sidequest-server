# Design Decision — WN ruleset-module worlds own magic via `core.spellcasting`/`core.effort`, NOT the ADR-126 `magic_state` plugin framework

**Date:** 2026-06-10
**Author:** Architect (The Man in Black), at Keith's direction ("whenever we get hit by this, take the Without Number rules, check epic 102")
**Status:** Accepted
**Context:** Story 90-3 (AC5b live free-play OTEL proof), Epic 102 (Complete the Without Number Family)

## The question

The 90-3 AC5b proof kept failing to show `wwn.spell.cast` for heavy_metal worlds, and
investigation surfaced two `magic_init` problems:

1. **AND-gate** (`server/magic_init.py:204`, dup `:322`) skips magic unless *both* a
   genre-level and a world-level `magic.yaml` exist. heavy_metal ships no genre
   `magic.yaml`, so every heavy_metal world skips → `snapshot.magic_state = None`.
2. **Schema mismatch.** Even if the gate were relaxed, `long_foundry/magic.yaml` is
   authored in the ADR-126 `magic:`-wrapped plugin schema, which the only production
   loader (`genre/magic_loader.py:load_world_magic`) cannot parse — it expects the flat
   `WorldMagicConfig` shape (top-level `world`, `ledger_bars`, `cost_types`, …).

This looked like it needed a loader/schema reconciliation with a content-direction fork.

## The decision

**It needs none of that. The `magic_init` / `magic_state` / `WorldMagicConfig` path is
NOT how WWN (and the rest of the WN family) does magic, and is OFF the AC5b path.**

WN ruleset-module worlds (WWN: heavy_metal, elemental_harmony, barsoom; SWN: space_opera;
CWN: neon_dystopia, road_warrior; AWN: mutant_wasteland) own magic through the **WN
`RulesetModule`**:

- Casting state lives on the **character**: `core.spellcasting` (prepared spells,
  `casts_remaining`/`casts_per_day`) and `core.effort` (source-keyed Effort pools).
- `WwnRulesetModule.resolve_spellcast` (`game/ruleset/wwn.py:466`) reads
  `caster_core.spellcasting` (`:499`) and `core.effort`, spends casts/Effort, applies
  System Strain, and emits **`wwn.spell.cast`** on every call.
- `grep magic_state sidequest/game/ruleset/*.py` is **empty** — the WN modules never read
  or write `snapshot.magic_state`.

The AC5b spellcast spine is therefore the WN cast spine, already delivered by **Epic 102**:
102-1 (PC-death downed seam on the reprisal path), 102-2 (in-combat `cast_spell` via the
dice path → `wwn.spell.cast`), 102-3 (explicit free-play cast classified as
`magic_working` → `resolve_spellcast`), 102-4 (WN turn model). All `done`.

### Corollaries (what NOT to do)

- **Do NOT relax the `magic_init` AND-gate as an AC5b fix.** It governs the separate,
  parallel ADR-126 plugin-framework magic, which WWN worlds do not use. (It may be a
  latent bug for packs that *do* use that framework — track separately, not here.)
- **Do NOT reconcile `long_foundry/magic.yaml`'s schema.** That 78 KB file is **orphaned
  draft content** in an incompatible (ADR-126 `magic:`-wrapped) schema. WWN magic for
  long_foundry comes from `core.spellcasting`/`core.effort`, seeded at chargen, not from
  this file.
- **Do NOT treat `magic_state: null` on a WWN world as a blocker.** It is expected and
  correct for ruleset-module worlds.

### Content + observability hygiene (separate, non-AC5b)

- **Quarantine/retire `long_foundry/magic.yaml`** (and any sibling orphaned ADR-126 world
  magic.yaml on a WN pack). It is dead draft content producing a misleading
  `magic.init_skipped reason=no_magic_yaml` signal that cost this investigation real time.
  Per *No Stubbing / No Dead Code*: delete it, or mark it `draft:` and exclude it from the
  bind path.
- **Consider a clearer GM-panel signal** for WN worlds — e.g. emit `magic.ruleset_owned`
  (component=magic) at bind for ruleset-module packs so the panel reads "magic owned by
  the WWN module" instead of the misleading "init_skipped". (Small, optional; OTEL
  Observability Principle.)

## The real remaining AC5b gaps (after Epic 102)

The spellcast/combat/lethality spine is done. What 90-3 still needs is upstream of it:

1. **Combat reachability.** A fight-seeker must actually reach a WWN combat confrontation
   in free-play. Measured (2026-06-10, `2026-06-10-evropi-2`): 11 rounds of aggressive
   actions, `active_confrontation: None` — the narrator correctly refused to fabricate a
   hostile in the social Upre Town Hall opening; statted hostiles (90-1) are "east," not
   co-located. Fix lane: **content** (a combat-reachable opening / co-located hostile) or
   accept navigation-to-hostiles, or run the proof via the headed DRIVER path.
2. **Caster statting.** The PC must have `core.spellcasting`/`core.effort` populated.
   Headed chargen (oq-1 Vesska / Elementalist) does this; the **headless scenario
   `strategy: auto` chargen ignored the scenario `class:`** and rolled a martial
   Quarter-Officer (no Calling, empty `spellcasting`). Fix lane: **orchestrator tooling**
   (`scripts/playtest.py` auto-chargen honors `class:`) — or run the proof headed.
3. **Live verification.** Re-run with a properly-statted caster who reaches combat;
   confirm `wwn.spell.cast`, the `wwn.*` combat spans + ablative HP, the
   `{ruleset}.mortal_injury/.shock` downed seam, and Effort/casts spend all fire — the
   lie-detector quiet.

## Implementation guidance (for Dev)

- Server: **no magic_init / loader change for AC5b.** Optional: the `magic.ruleset_owned`
  signal + retiring the dual `magic_init` AND-gate's misleading `init_skipped` for WN
  packs.
- Content: quarantine `long_foundry/magic.yaml`; add/confirm a combat-reachable opening or
  co-located hostile for the AC5b worlds.
- Tooling (orchestrator): `scripts/playtest.py` headless chargen must honor the scenario
  `character.class` so caster scenarios produce casters.
- Then re-run the AC5b live proof (headless once chargen honors class, or headed) and
  capture the WN spans.
