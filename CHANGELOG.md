# Changelog

All notable changes to the SideQuest game-engine backend.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-05-26

### Added
- **Intent Router — mechanical-engagement spine (ADR-113)** — atomic cutover
  retiring `begin_confrontation`; the router emits its `DispatchPackage` via
  ADR-102 tool-use (not free-text JSON), feeds confrontation type vocabulary
  to the classifier, and carries a router-vs-engine lie-detector watcher that
  retires the old reprompt loop (59-2/59-3/59-4/59-10).
- **LocalDM dispatch subsystems** — `scenario_clue`, `magic_working`, and the
  three LocalDM subsystems wired onto the live dispatch path; magic-working
  sidecar retired (59-5/59-6/59-7).
- **Ablative HP substrate (ADR-114)** — HP reclaims the lethality track beneath
  the dials.
- **Per-PC dungeon movement subsystem** — `run_movement_dispatch` with per-PC
  region data model and per-PC `dungeon.map_emitted` (movement Phases 1-3).
- **SaveRepository persistence layer (ADR-115 P0)** — `SaveRepository` +
  `SaveTransaction` protocols and a `SqliteSaveRepository` unit-of-work adapter;
  `EventLog` and `ProjectionCache` now delegate through it.
- **Reference pages** — Rules + Lore HTML routes with theme injection, CSS
  routes, hero banner, contents rail, body presenters, visibility whitelist,
  POI landscape images on lore pages, and per-entity `reference_url` fields on
  protocol models (Location/Journal/PartyMember/AbilityDefinition) with OTEL
  spans and anchor islands (epic 63, reference epic).
- **Seed trope engine** — engagement-triggered seed draws, OTEL routing with
  `SPAN_SEED_FIRED`, and VALLEY-zone narrator injection with the Faded-ghost tag
  (22-3/22-4/22-5).
- **Pack schema validation** — pack file contents validated through pydantic
  models with a `pf validate` schema validator, model fields, and corpus
  fallback (64-4, plus reference-chrome validator 63-5).
- **MP turn barrier release on client render-crash signal** (67-1).
- **Confrontation lifecycle lie-detector** — narrator kill claims checked
  against engine state; chase confrontations now seat an opponent (ADR-116
  movement slice, 59-13).
- **Scene fixture picker** — DEV_SCENES gate removed; listing endpoint added (51-4).
- **rig_composure + injury_tags** on the PartyMember protocol with rig OTEL
  spans (53-4/53-5).
- World `navigation_mode` surfaced in `/api/genres`; class-move beat ids
  resolved to labels + descriptions (rest/views).

### Changed
- **Committed to the SDK narrator** — `claude`/Ollama narrator backends retired
  (61-9); byte-static narrator prose promoted to the System cache bucket and
  `iter=1` user message marked with a cache_control marker for cost control
  (61-10/60-7).
- **ADR-110 snapshot slimming follow-through** — Phase C projections slim seven
  growing fields, a snapshot field-governance gate added, hard-cap oversized
  canary on SDK + sync paths, and a cost-runaway suspected alarm in
  `anthropic_sdk_client.py` (61-2/61-3/61-4/61-5).
- **Module decomposition (epic 64)** — broke the `session_handler` import cycle
  via a `session_state` leaf module; decoupled theme-wiring tests from live
  content.
- Genre chargen scene-gated via `opening_directive` (partial reversal of
  ADR-112, 61-11); `output_only.md` prose compacted with magic rules extracted.

### Fixed
- **Persistence write safety** — all save-DB writes serialized via
  `SAVE_WRITE_LOCK`, including holding the lock for the load-path WAL checkpoint
  (#413, persistence).
- **`close_store` clean re-bind** — nulls `_snapshot`/`_session` so reconnects
  don't build on a stale snapshot; `close_store()` wired into the
  last-disconnect teardown path (#411, 61-followup-C).
- Intent-router resilience — confrontation `params['type']` contract, watcher
  no longer crashes WS turn-delivery, empty-response failures diagnosed with an
  env-gated degrade unblock (#448/#410).
- MP fixes — POV-correct joiner arrivals for the 3rd+ PC, scene-location change
  propagated to the co-located cohort, canonical sealed-letter roster emitted on
  every broadcast (#417/#418/#420).
- Opening rebinds `current_region` from `opening.setting.region_id` (#449);
  region-aware location self-heal (#366).
- Reference-page polish — broken TOC/content duplication, oversized hero banner,
  `present_magic` no longer dumps raw config, `locations.yaml` enumerated in
  `LORE_WORLD_FILES`.
- Universal confrontation beats bypass the `encounter_beat_choices` whitelist;
  freeform chargen input routed to `answer_followup` in `AwaitingFollowup`.
- Narrator ADR-105 B3 public-safe enforcement (mechanical scrub); POV-swap
  writes the PC's name instead of antecedent-blind pronoun passes.

## [1.2.0] - 2026-05-23

This release is large (~330 non-merge commits) and dominated by net-new
subsystems alongside heavy internal restructuring. The headline shifts are the
**Anthropic SDK narrator migration** (ADR-101/102 becoming the live default),
the **runtime procedural megadungeon** (ADR-106, `beneath_sunden`), the
**forensics save-inspection surface**, and a large body of OTEL/telemetry and
test-wiring hardening.

### Added
- **Anthropic SDK narrator migration (ADR-101/102)** — SDK narrator path made
  live and flipped to the default backend; native tool-use replaces the JSON
  sidecar. `ToolingLlmClient` routes to the SDK path; narrator cache cost
  reduced via tools 1h cache + 5m/1h telemetry, per-block cache attribution on
  `prompt_assembled`, and a moving 1h cache_control breakpoint on tool-loop
  continuation (60-2/60-4).
- **Runtime procedural Jaquaysed megadungeon (ADR-106)** — the bulk of this
  release. Full generator family ported (recursive-backtracker, randomized-Prim,
  cellular, room-and-corridor) with braid post-processing; region-graph data
  model with typed edges, BFS/cyclomatic checks, and an exact Jaquays invariant
  checker; deterministic blake2b sub-seeding and depth jitter; `DungeonStore`
  with a 5-table additive schema, append-only mutation overlay + ordered replay,
  frontier persistence, and a complication ledger; the five-stage materialize
  pipeline (design → fill → curate → attach → commit) with an async look-ahead
  worker; depth scoring with player-facing `level_bucket`/`level_phrase`; set
  pieces, trope-start-at-attach, and quest-seed seams; `DungeonTheme`/
  `ThemePalette`/`InteriorSpec` models with fail-loud loaders; session
  attach/detach with resume self-heal and a save-keyed double-register guard.
- **Region cookbook** — `CookbookBundle` loader over the `beneath_sunden` world
  dir; SRD→corpus ingest transform with CR parsing; deterministic per-region
  manifest assembly with CR-band + size-budget resolution, affinity-weighted
  RACE rolls, SPECIAL-room selection, depth-gated BIG BAD capstone, wandering +
  loot table builders, and a `world_register` curation hard filter; OTEL spans
  for region assembly.
- **Forensics save-inspection surface** — `/api/debug/saves`, timeline, and turn
  endpoints; `list_saves`, `build_timeline` (round-keyed bucketing),
  `build_turn_bundle` (five drill-down panels); pure `StateDelta`/mechanical
  census/telemetry folds; a Tufte-redesigned forensics page UI with macro strip,
  comparison block, and per-round mechanical/telemetry lanes; read-only
  `?mode=ro` snapshot endpoint so the panel can't mutate saves.
- **Scenario system (ADR-053)** — `discover_clue` wired to narration with
  ClueGraph DAG prerequisite enforcement; `GossipEngine` two-phase belief
  propagation with contradiction detection and credibility decay;
  `AccusationEvaluator` with dispatch wiring; `KnownFact.confidence` enum.
- **Location subsystem (ADR-109)** — `LocationEntity` types +
  `LOCATION_DESCRIPTION` message, `pf validate locations` validator,
  `resolve_location_entity` tool with promotion + `location_promotions` table,
  encounter location overlays with read-time merge + `LOCATION_OVERLAY_CHANGED`,
  and a `location.*` OTEL span family.
- **Scene harness (ADR-092)** — dev-gated `POST /dev/scene/{name}` hydrator that
  populates `known_facts`, `scenario_state`, `StructuredEncounter`,
  `magic_state` + abilities, and multi-PC character lists from fixtures (50-18
  through 50-24).
- **Monster Manual (ADR-059)** — subsystem ported back to Python.
- **Disposition system (ADR-020)** — central `Attitude` enum + `attitude()`;
  `before/after/crossed` emitted on `disposition.shift`; coarsened attitude band
  in the narrator NPC roster; genre-configurable thresholds (50-10 through 50-13).
- **World grounding (24-x)** — weather generator + CLI, narrator tool call for
  weather/demographics/calendar grounding, bootstrap loaders + `ToolContext`
  fields, and `world_grounding` OTEL spans.
- **Out-of-band aside channel (ADR-107)** — non-turn-consuming server channel.
- **Cavern renderer (ADR-096)** — per-region mask + derived block emit,
  mask-BLOB persistence + loader, `emit_runtime_cavern_png` with tactical-grid
  runtime wiring and a runtime/static source discriminator.
- **Rig composure pool (ADR-031/053)** — `RigComposurePool` with OTEL spans,
  vessel-materializer binding, and a crash handler (Composure→0 fires injury +
  Edge −1 + dismount).
- **Mechanical census telemetry** — per-seated-PC + session trope census emitted
  inside the C2 NARRATION transaction, with canonical-state projections.
- **NPC pipeline** — auto-mint NPCs from prose-only dialogue mentions, a
  ratification gate before pool persistence, and location-patch enforcement when
  narrator titles drift from state (49-2/49-3/49-6).
- **Narration POV swap + visibility classifier** and per-PC beat projection
  filtered on outbound (49-7/49-8).
- **Prompt cost / recency tuning (ADR-110/112)** — recency window K=4→K=2, four
  genre prose sections promoted into the cached Stable block, recency guardrails
  migrated to tool descriptions, and game-state snapshot slimming Phase A/B
  (57-1/57-3/57-4/57-5).
- A/B eval harness + operator CLI and genre-tagged Ollama narrator routing
  (48-3/48-4).

### Changed
- **Intent validator replaces the prose lie-detector** — per-`ConfrontationDef`
  intent verb sets derived at pack-load, a pure `validate(...)` function, and
  dispatch wiring (intent epic).
- **Test-wiring hardening** — pytest-xdist parallel suite adopted, source-text
  regex-scanner wiring tests dropped and the prohibition lifted into CLAUDE.md,
  load-bearing census/telemetry/forensic wiring invariants pinned (50-27).
- Narrator prompt sections extracted to sibling `.md` files (#252); integration
  tests re-pointed off the deprecated `caverns_sunden` world; intent/dungeon
  Protocol typing tightened.
- Dice authority threaded `player_action` through `DICE_THROW` and resolves the
  rolling PC from `player_seats` (50-24/dice).

### Fixed
- **Narrator cache cost** — restored 1h ephemeral cache TTL + beta header
  (cost fix); `--tools ""` passed to all `claude` spawn sites to skip tool
  loading (#253/#254).
- **MP attribution (ADR-105/108)** — emitters project the merged-MP driver (not
  the raw bypass), per-turn XP is party-wide rather than host-only, and MP item
  attribution uses per-recipient tagging.
- Region-mode/cartography worlds projected into the narrator prompt with
  region-aware self-heal and `LOCATION_DESCRIPTION` wiring (#366/region).
- Chargen `out` accumulator bound before emit closures fire; `{class}` prose slot
  filled for free-text vocations.
- `namegen` resolves `names_file` in `corpus_dir` and drops the trailing period
  from spelled-out honorifics (#268).
- Footnote `fact_id` minted on the replay/backfill path (ADR-100 Seam C);
  KnownFacts footnotes folded correctly in forensics.
- Ungraceful WS drop logs as a clean INFO teardown rather than
  `ws.unexpected_error`.

### Removed
- Dead module-level `run_narration_turn` wrapper (49-5).

## [1.1.0] - 2026-05-11

### Added
- **World items.yaml loader** — surfaces `named_items`, `modifier_items`,
  `reliquaries`, `crimson_remnants`, and `consumable_items` to `World.items`
  as a `WorldItemsCatalog`. Loud-fails on duplicate ids across sections,
  malformed YAML, or missing id/name. Emits `state_transition:world_items:loaded`
  OTEL watcher event with per-section counts.
- **Cleric divine_favor reliquary invocation** — `invoke_reliquary` op with
  four typed-reason gates (no_divine_favor_bar, favor_below_threshold,
  unknown_reliquary, reliquary_missing_effect, free_use_already_spent),
  default threshold 0.7, once-per-session token tracked on
  `MagicState.reliquary_free_use_spent`. Emits `magic.invoke_reliquary`
  watcher event on success.
- **Narrator reliquary context** — `build_magic_context_block` now renders
  an `<available-reliquaries>` section carrying each eligible reliquary's
  verbatim `divine_favor_effect` text when the Cleric passes all gates.
  `TurnContext.world_items` plumbed through `run_narration_turn`.
- **Stateless narrator (ADR-098)** — narrator turns no longer use
  `--resume`; each turn ships a bounded per-turn prompt. Stale degraded-
  result tests retired.
- **MP TURN_STATUS broadcast** — per-player turn status with claude CLI
  stderr capture (47-5).
- **B/X B26 saving throws** — schema + resolver + OTEL coverage; per-class
  saving-throw tables required when the pack ships spell catalogs.
- **C&C B/X class beats** — beat filters by class; `prepare`/`cast`/`rest`/
  `turn_undead` ops on `learned_v1`; per-spell-level slot ledger bars.
- **Cold-subsystem OTEL coverage** — genre pack load, cache hit/miss,
  unrouted magic costs, and items catalog load all emit watcher events.
- **Cinematic narrator default** — verbosity/vocabulary defaults shipped
  for live play.

### Changed
- Audio config path resolution drops `Path.exists()` guard so R2-only
  packs resolve via URL.
- Chargen Edge seed += CON modifier (story 39-9 / 39-10).
- Confrontation difficulty calibration v1 (ADR-093).
- `MagicState.spent_spells` exposes UI strikethrough state for cast
  spells until rest.

### Fixed
- `_degraded_result` correctly sets `is_degraded=True`.
- ACE-Step output fields stop creeping back into music params JSON via
  output-only treatment in daemon.

## [1.0.0] - prior

Initial Python port from the Rust `sidequest-api` prototype per ADR-082.
Not formally tagged at the time; recorded here for continuity.
