# Dungeon Region Population — Region-Keyed Inject + Co-location-by-Region

**Date:** 2026-06-22
**Author:** Architect
**Status:** Proposed (design before code, per Keith's ruling)
**Epic:** 153 (continues 153-23 DUNGEON-ROOM-POPULATION-INERT)
**Amends:** ADR-059 (Monster Manual inject), ADR-106 (procedural megadungeon),
ADR-116 / ADR-139 (a confrontation requires a co-located Other)

---

## 1. Problem

The procedural megadungeon (ADR-106, `beneath_sunden`) generates a creature
roster **and** a big-bad for every region during the curate stage, and then
throws them away. Two breaks plus one shared root cause:

**Gap A — generated populations are computed and discarded.**
- `materializer.py:1404-1530` (`_stage_curate`) fills
  `RegionCuration.region_creatures[region_id]` and `region_big_bad[region_id]`.
- The accessors `creatures_for_region()` / `big_bad_for_region()`
  (`materializer.py:512-524`) have **zero callers** (`grep` confirms).
- The room-YAML emit (`room_yaml_emit.py:66-72`, `materializer.py:2116-2122`)
  writes only `{room_type, name, description, entities}` — never
  `encounter_creatures`. But the runtime binding resolver
  (`room_creature_binding.py:80`) **reads** `encounter_creatures`. The
  generator never writes the key the runtime reads.
- Net: only **hand-authored** `encounter_creatures` bindings (e.g. the
  entrance's `gnaw_swarm`) reach runtime. Procedural rooms (`exp00N.rN`) field
  an empty pool → narrator improvises monsters with no engine backing →
  fabricated HP / invented rules persisted as Lore.

**Gap B — the authored creature that *does* surface gets a stale location.**
- Inject call site `websocket_session_handler.py:838-862`:
  `mm_location = turn_context.current_location` is the **pre-dispatch** free-text
  scene; `mm_room_id = snapshot.region_for()` is the **post-dispatch** region id
  (movement dispatch already ran at `movement.py:497`).
- `monster_manual_inject.py:419,534` binds the creature to the new `room_id`
  but stamps `location=current_location` (the *old* scene). On a movement turn
  the creature carries the previous room's name.
- Seating co-location (`encounter_lifecycle.py:948/1023/1127`) compares
  `npc.last_seen_location`/`npc.location` against `party_location(pc)` — both
  free-text scene strings → mismatch → `NoOpponentAvailableError`
  (`:1610`) → dispatch declined → free narration. Spans fire
  (`encounter.no_opponent_available`, `dispatch_engagement.confrontation.mismatch`).

**Root cause.** Mechanical placement and seating co-location key off the
**narrator-owned free-text scene** (`character_locations`, set late in
`narration_apply` from prose title-scraping) instead of the **engine-owned
region id** (`pc_regions` / `region_for`, set early and deterministically).
Whether combat seats in a procedural dungeon depends on the narrator typing the
exact room title.

## 2. Decision

**Option 1 (ruled by Keith, 2026-06-22):** procedural rooms auto-populate with
the curated roster via a **region-keyed direct inject**, and seating
co-location becomes **region-aware**. Procedural `CuratedCreature`s are
free-text (name / type / telegraph / HP-from-CR, `materializer.py:965-1019`) —
they are NOT bestiary entries, so they cannot ride the authored
`encounter_creatures`→bestiary path. They inject directly as NPC patches keyed
by `region_id`, the way `_creature_patch_from_enemy` already materializes
encountergen rows.

Procedural population is **per-save procedural state** → it persists in the
dungeon store (Postgres), NOT in the authored content repo's room YAML. This
keeps the content/save boundary intact: authored creatures live in
`worlds/<world>/rooms/<id>.yaml#encounter_creatures`; procedural creatures live
in the per-save dungeon store keyed by `region_id`.

## 3. Design

### Part A — Persist the curated roster (region-keyed, frozen)

Reuse the existing freeze-persistence seam — **no new table, no Alembic
migration.** `DungeonRepository` already exposes
`record_mutation(region_id, kind, payload)` and `load_mutations() ->
list[DungeonMutation]` (`repository.py:293,344`; `pg/dungeon.py:491,496`), used
today for setpiece rolled-state.

- **Write:** in the commit stage (Task 6, where setpiece state is already
  frozen), for every region in the expansion call
  `record_mutation(region_id, kind="region_population", payload=...)` with the
  serialized `region_creatures[region_id]` + `region_big_bad[region_id]`.
  `CuratedCreature` is a dataclass → a `to_payload()` / `from_payload()` pair
  (name, creature_type, telegraph, hp pool, **cr/threat — see model change
  below**). Frozen exactly once at materialize; save-is-truth on resume.
- **Read:** a thin loader keyed by `region_id` over `load_mutations()` filtered
  to `kind="region_population"`, memoized per turn (the curate roster is frozen,
  so this is a pure read).

This deletes the dead-output smell: `creatures_for_region()` /
`big_bad_for_region()` gain a real persistence consumer.

### Part B — Inject by region_id

Add a procedural sibling to the authored room-binding path in
`monster_manual_inject.py`, parallel to `_npc_patches_for_room_binding`
(`:511`):

```
_npc_patches_for_region_population(sd, region_id, *, location, region) -> list[NpcPatch]
```

- Loads the frozen roster (Part A) for `region_id`.
- Maps each `CuratedCreature` → `NpcPatch` via a new
  `_creature_patch_from_curated(curated, *, location, region)`, mirroring
  `_creature_patch_from_enemy` (`:434`): `name`, `hp` (from HpPool), `role` /
  `description` from `telegraph` / `creature_type`, `threat_level` from the
  retained CR band, `manual_origin=True`, **`region=region_id`** (new field).
- The big-bad injects as one elevated-threat patch flagged distinctly (role
  marker), telegraphed but gated — see §5 (Keith decision: depth gate).

Wire it into `inject()` (`:546`) alongside the existing room-binding call,
behind the same `room_id is not None` gate. The authored path
(`encounter_creatures`) and the procedural path (`region_population`) are
**additive and de-duplicated by name** — an authored binding always wins
(matches `_append_authored_creatures`' existing precedence at `:1104`).

### Part C — Co-location by region (the seating fix)

This is the load-bearing change and the one that touches ADR-116 / ADR-139.

**Model change (§4):** add `region: str | None` to `Npc` and `NpcPatch`.

**Seating predicate** (`encounter_lifecycle.py:948,1023,1127`): co-location
prefers region, falls back to scene:

```
pc_region = snapshot.region_for(perspective=acting_character_name)
if npc.region and pc_region:
    co_located = (npc.region == pc_region)          # engine-owned, deterministic
else:
    co_located = (npc.last_seen_location == location # today's free-text path
                  or npc.location == location)
```

This is **self-gating**: an NPC with no `region` stamp (every narrator-declared
NPC, every NPC in the 11 non-procedural worlds) takes the existing free-text
path unchanged. Only region-stamped procedural creatures use region comparison.
**Blast radius is the procedural dungeon only** — oz / wonderland / gulliver /
orbitals and all static-cartography worlds are untouched because their NPCs
carry no `region`. This is the scoped form of the "engine owns region"
direction from the 2026-06-22 sünden-crossing decision — explicitly NOT the
system-wide re-arch that decision warned against.

### Part D — Location stamp consistency (Gap B)

Part C makes seating independent of the stale free-text stamp, but the
free-text `location` still feeds the narrator prompt and UI, so it must be
right. At the inject call site (`websocket_session_handler.py:838-862`) stamp
**both** from the same post-dispatch source:
- `region = mm_room_id` (already post-dispatch `region_for()`).
- `location` = the PC's post-dispatch resolved scene (`party_location(pc)`
  computed *after* the dispatch bank), not the pre-dispatch
  `turn_context.current_location`.

### Model changes

| Model | Field | Notes |
|-------|-------|-------|
| `Npc` (`session.py:130`, `extra:forbid`) | `region: str \| None = None` | Rides snapshot JSON blob; `default=None` → no migration (same pattern as `aliases`). |
| `NpcPatch` (`session.py:370`, `extra:forbid`) | `region: str \| None = None` | Carried through `Session._npc_from_patch`; monotonic merge like `manual_origin`. |
| `CuratedCreature` (`materializer.py`) | retain `cr` / `threat_level` | Currently dropped after `_hp_from_cr`; needed so the inject can stamp `threat_level`. |

### OTEL (lie-detector — mandatory)

- `dungeon.region_population_injected` — `{region_id, creature_count, big_bad: bool}` on the Part B inject.
- `confrontation.colocation` — `{match_mode: region|scene, pc_region, npc_region, co_located: bool}` on the Part C predicate, so the GM panel shows *which* co-location path fired and why seating did/didn't happen. This is the missing signal that let Gap B hide.
- Keep the existing `encounter.no_opponent_available` / `dispatch_engagement.confrontation.mismatch` spans; the new `confrontation.colocation` span explains them.

### Persistence / freeze / resume / migration

- Roster frozen once at materialize via `record_mutation` (save-is-truth).
- **Already-materialized saves** have no `region_population` mutation rows → their existing regions stay empty (acceptable: new expansions populate; authored bindings still fire). If Keith wants existing saves backfilled, a one-shot re-curate pass is a follow-up — flagged, not assumed.

## 4. Blast radius

**Touches:** `materializer.py` (commit-stage write + `CuratedCreature.cr`),
`monster_manual_inject.py` (procedural inject path + `region` stamp),
`encounter_lifecycle.py` (region-aware co-location), `session.py`
(`Npc`/`NpcPatch.region`), `websocket_session_handler.py` (stamp source),
new spans.

**Does NOT touch:** the authored `encounter_creatures` path (107-2), the region
graph topology model, the content repo room YAML, the 11 non-procedural worlds
(self-gated by absent `npc.region`), Fate/WN ruleset dispatch.

## 5. Crunch rulings (Keith, 2026-06-22)

1. **Big-bad placement: present, telegraphed.** Inject the big-bad into its
   region from turn one and foreshadow it via its `telegraph`, but seating still
   requires the player to engage — no ambush-seat on entry.
2. **Threat banding: derive from CR band.** Reuse the same CR→threat mapping the
   rest of the dungeon uses (retained on `CuratedCreature`). One curve, consistent
   with the authored bestiary — no separate procedural curve.
3. **Roster size: cap out-of-combat like today.** Reuse the existing
   `_OUT_OF_COMBAT_ENCOUNTER_LIMIT` so a calm room doesn't materialize a full mob;
   in-combat injects the full eligible roster (matches the authored path).

## 6. Test plan (wiring-first)

- **Wiring test (the point):** materialize a region → assert a
  `region_population` mutation row exists → drive a turn with the PC seated in
  that region → assert the curated creature is in `snapshot.npcs` with
  `region == region_id` → assert `dungeon.region_population_injected` fired.
- **Seating regression:** PC + region-stamped creature in same region → drive
  an attack → assert confrontation **seats** (no `NoOpponentAvailableError`) and
  `confrontation.colocation` fired with `match_mode=region`.
- **Gap B regression:** movement turn that changes region → assert the injected
  creature's `region` equals the post-dispatch region (not the prior one).
- **Non-regression:** a narrator-declared NPC (no `region`) in a non-procedural
  world still co-locates via the free-text path (`match_mode=scene`).
- **No source-text wiring tests** (CLAUDE.md) — all assertions are span- or
  fixture-behavior-driven.

## 7. Sequencing

1. Model fields (`Npc`/`NpcPatch.region`, `CuratedCreature.cr`) + `_npc_from_patch` carry.
2. Part A persist (commit-stage write + loader) + wiring test.
3. Part B inject path + span.
4. Part C region-aware co-location + `confrontation.colocation` span + seating test.
5. Part D stamp-source fix.
6. Full `just server-check`.
