# CLAUDE.md — SideQuest Server (Python)

Python FastAPI game engine for SideQuest. Live backend on port 8765. Ported from
the Rust prototype `sidequest-api` per ADR-082 (2026-04-19).

## CRITICAL: Personal Project

This is a personal project under the `slabgorb` GitHub account.
- **No Jira integration.** Never create, reference, or interact with Jira tickets.
- **No 1898 org.** Nothing goes to the work GitHub org. Ever.
- All repos live under `github.com/slabgorb/`.

## SideQuest System Overview

Four repos compose the SideQuest stack:
- **sidequest-server** *(this repo)* — Python/FastAPI game engine and WebSocket API on port 8765
- **sidequest-ui** — React/TypeScript game client (Vite, port 5173)
- **sidequest-daemon** — Python media services (Z-Image image gen, ACE-Step music)
- **sidequest-content** — Genre packs (YAML configs, audio, images, world data)

Orchestrator repo (`orc-quest`, also cloned as `oq-1` / `oq-2`) coordinates sprint tracking, docs, ADRs, and cross-repo scripts.

## Quality Rules

- No stubs, no hacks, no "we'll fix it later" shortcuts
- No skipping tests to save time
- No half-wired features — connect the full pipeline or don't start
- If something needs 5 connections, make 5 connections. Don't ship 3 and call it done.
- **Never say "the right fix is X" and then do Y.** Do X.
- **Never downgrade to a "quick fix" because you think the context is "just a playtest."**
  Every playtest is production tomorrow. Fix it right.

## Development Principles

### No Silent Fallbacks
If something isn't where it should be, fail loudly. Never silently try an alternative
path, config, or default. Silent fallbacks mask configuration problems and lead to
hours of debugging "why isn't this quite right."

### No Stubbing
Don't create stub implementations, placeholder modules, or skeleton code. If a feature
isn't being implemented now, don't leave empty shells for it. Dead code is worse than
no code.

### Don't Reinvent — Wire Up What Exists
Before building anything new, check if the infrastructure already exists in the codebase.
Many systems are fully implemented but not wired into the server or UI. The fix is
integration, not reimplementation.

### Verify Wiring, Not Just Existence
When checking that something works, verify it's actually connected end-to-end. Tests
passing and files existing means nothing if the component isn't imported, the hook isn't
called, or the endpoint isn't hit in production code. Check that new code has non-test
consumers.

### Every Test Suite Needs a Wiring Test
Unit tests prove a component works in isolation. That's not enough. Every set of tests
must include at least one integration test that verifies the component is wired into the
system — imported, called, and reachable from production code paths.

### No Source-Text Wiring Tests
**Never grep production source code as a wiring assertion.** Tests like "assert
`_my_function(` appears N times in handler.py" or "assert this regex matches the
source of narrator.py" test *implementation shape*, not *behavior* — they pass when
the literal happens to be present even if the wiring is broken, and they fail on every
harmless refactor. Worse, regexes with `re.DOTALL` plus `.*?` quantifiers against
large source files can catastrophically backtrack inside C code that holds the GIL,
defeating `pytest-timeout` and turning a test into a true hang.

When you want to prevent a call-site regression, reach for one of:

1. **OTEL span assertions** — every subsystem decision emits a span (CLAUDE.md OTEL
   Observability Principle, ADR-031 / ADR-090 / ADR-103). Drive the flow, assert the
   span fired. Survives refactor, fails on real wiring breakage.
2. **Fixture-driven behavior tests** — construct a synthetic state that should hit the
   path, fire the dispatch through the real handler, assert the message went out. See
   `tests/server/test_location_description_emit.py::test_emit_sends_message_when_room_has_manifest`
   for the canonical shape — synthetic genre pack + snapshot fixture + real
   `_maybe_emit_location_description` invocation + assertion on the emitted typed
   message.
3. **Registry / decorator dispatch** for load-bearing wiring — `@on_chargen_complete(...)`
   instead of nested `if/elif` blocks deep in a handler. Then a unit test enumerates the
   registry. Refactor-stable. Heaviest lift; only worth it when the wiring keeps slipping.

The legitimate exception is **reflection-based dataclass / type checks** (e.g.,
"`_SessionData` does not yet have a `dungeon_store` field — `inspect.fields(...)`")
because those interrogate runtime types, not source strings. That's the "tripwire"
pattern in `tests/dungeon/test_setpiece_attach_wiring.py`'s assertion 4 — fine.

If you find yourself reaching for `handler_path.read_text()` in a test, stop. The
infrastructure to do it properly already exists in the codebase.

### Backend Language
This server is Python/FastAPI per ADR-082, ported from a Rust prototype in 2026-04.
The Rust codebase is preserved read-only at <https://github.com/slabgorb/sidequest-api>
for historical reference; older ADRs that show Rust code are historical illustration
only — see `orc-quest/docs/adr/README.md` for the translation table. New backend code
goes in Python. Media services (`sidequest-daemon`) remain Python for inference
library maturity (Z-Image / ACE-Step). The narrator LLM path uses the **Anthropic
Python SDK** by default per **ADR-101** (which supersedes ADR-001): backend
selection is `SIDEQUEST_LLM_BACKEND`, defaulting to `anthropic_sdk` — see
`sidequest/agents/llm_factory.py` and `sidequest/agents/anthropic_sdk_client.py`.
The `claude -p` CLI subprocess (`claude_client.py`) and Ollama remain opt-in
non-default backends, and `claude -p` still serves some non-narrator jobs (e.g.
the dungeon "curate" stage). TTS was removed from the system in 2026-04.

## OTEL Observability Principle

Every backend fix that touches a subsystem MUST add OTEL watcher events so the GM panel
can verify the fix is working. Claude is excellent at "winging it" — writing convincing
narration with zero mechanical backing. The only way to catch this is OTEL logging on
every subsystem decision:

- **Intent classification** — what was the action classified as, and why?
- **Agent routing** — which agent handled the action?
- **State patches** — what changed in game state (HP, location, inventory)?
- **Inventory mutations** — items added/removed, with source
- **NPC registry** — NPCs detected, names assigned, collisions prevented
- **Trope engine** — tick results, keyword matches, activations
- **Encounter engine** — beat selections, metric changes, resolution
- **Magic / class abilities** — when a power activates, with cost and effect

The GM panel is the lie detector. If a subsystem isn't emitting OTEL spans, you can't
tell whether it's engaged or whether Claude is just improvising.

**Not needed for:** Cosmetic changes (label rewording, log message tweaks).

## Build Commands

```bash
uv sync                            # Install deps
uv run pytest -v                   # Tests (parallel by default: -n auto via addopts)
uv run pytest -n0 -v               # Tests (serial; for breakpoint debugging / race-isolation)
uv run ruff check .                # Lint
uv run ruff format .               # Format
uv run pyright                     # Type check
```

The unit suite runs under pytest-xdist (`-n auto`) by default — full suite is ~20-30 s on a 10-core machine. Pass `-n0` to override for interactive debugging where you need a single process and predictable test ordering.

From the orchestrator root: `just server`, `just server-test`, `just server-check`, `just server-fmt`.

## Architecture

The package layout mirrors the prior Rust crate layout 1:1 (load-bearing per ADR-082):

```
sidequest/
├── protocol/         # GameMessage discriminated union, typed payloads
├── server/           # FastAPI app, WebSocket, dispatch, sessions, watcher
│                     #   intent_router_pass.py — execute_intent_router_pre_narrator_pass
│                     #   (called from websocket_session_handler.py before the narrator)
├── handlers/         # Per-message-type dispatch handlers
├── agents/           # Anthropic SDK narrator (default) + claude -p/Ollama opt-in backends
│                     #   intent_router.py — IntentRouter (pre-narrator Haiku-via-SDK pass →
│                     #     DispatchPackage); subsystems/ — dispatch handlers + run_dispatch_bank;
│                     #   dispatch_engagement_watcher.py — post-narration lie-detector
│                     #     (emits dispatch_engagement.{subsystem}.mismatch OTEL spans)
├── game/             # ~70 modules — state, combat, chase, NPCs, OCEAN, lore, etc.
│                     #   pg/ — Postgres repositories (PgSaveRepository, PgDungeonRepository,
│                     #     PgTelemetrySink, PgForensicReader + events/snapshot/narrative/
│                     #     scrapbook/asset_ledger/promotions/sessions sub-stores)
│                     #   db_config.py, db_pool.py — connection config + psycopg_pool
│                     #   importer.py — read-only legacy SQLite→Postgres importer
│                     #   ruleset/ — pluggable SRD ruleset modules (registry.py, base.py
│                     #     RulesetModule ABC, native.py default, swn.py Stars Without Number)
│                     #   creature_core.py — HpPool ablative HP on CreatureCore (Character + Npc)
├── genre/            # YAML loader, layered genre/world pack models
├── audio/            # Music + SFX coordination
├── media/            # Image generation orchestration
├── magic/            # Magic system mechanics
├── interior/         # Room / interior state
├── orbital/          # Orbital / space-scene mechanics
├── corpus/           # Conlang corpus + Markov naming
├── renderer/         # Render scheduling + throttle
├── daemon_client/    # Unix-socket client for the media daemon
├── telemetry/        # OTEL span definitions and watcher hooks
└── cli/              # encountergen, loadoutgen, namegen, validate, corpus*
```

**Dependency graph:**

```
sidequest.server
  ├── sidequest.agents      (depends on protocol)
  ├── sidequest.game        (depends on protocol, genre)
  ├── sidequest.daemon_client
  └── sidequest.protocol
```

### Persistence (ADR-115, complete)

Saves and all session-scoped state live in a single PostgreSQL database, reached
through `sidequest/game/pg/` (`PgSaveRepository`, `PgDungeonRepository`,
`PgTelemetrySink`, `PgForensicReader`, plus events/snapshot/narrative/scrapbook/
asset_ledger/promotions/sessions sub-stores). Connections come from
`db_config.py` + `db_pool.py` (psycopg3 + `psycopg_pool`); per-session row locks
serialize writes. DDL is owned entirely by Alembic (`alembic.ini`,
`alembic/versions/0001_initial_unified_schema.py`,
`0002_asset_ledger.py`) — do not hand-write `CREATE TABLE`. `SIDEQUEST_DATABASE_URL`
is **required** with no silent default (fail-loud per the No Silent Fallbacks rule).
The legacy SQLite write layer has been **deleted**; SQLite survives only as a
read-only import *source* via `sidequest/game/importer.py`.

### Pluggable rulesets (ADR-033/-114, live)

`sidequest/game/ruleset/` holds pluggable SRD ruleset modules behind the
`RulesetModule` ABC (`base.py`), resolved through `registry.py`. A pack binds one
via `ruleset:` in its `rules.yaml`; an unknown name raises `UnknownRulesetError`
(fail loud). Two modules are implemented: `native.py` (the dial/confrontation
engine, ADR-033 — default) and `swn.py` (Stars Without Number). Ablative HP
(`creature_core.py`) layers `HpPool` (`current`/`max`/`base_max`) onto
`CreatureCore`, shared by `Character` and `Npc`: damage flows through the strike
channel, 0 HP triggers the `hp_depletion` win condition, and each delta emits a
`state_patch_hp` OTEL span. **ADR-114 is partial** — only Part 1 is live.

### Intent Router (ADR-113, live/partial)

`IntentRouter` (`sidequest/agents/intent_router.py`) is a pre-narrator
Haiku-via-SDK pass that decomposes each player action into a `DispatchPackage`.
`execute_intent_router_pre_narrator_pass` (`sidequest/server/intent_router_pass.py`)
runs it from `websocket_session_handler.py` *before* the narrator so the
mechanical engines engage first, then `run_dispatch_bank`
(`sidequest/agents/subsystems/`) fires the matching dispatch handlers
(confrontation, magic_working, scenario_clue, npc_agency, distinctive_detail_hint,
reflect_absence, movement). After narration, `dispatch_engagement_watcher.py`
acts as a lie-detector, emitting `dispatch_engagement.{subsystem}.mismatch` OTEL
spans when prose claims a subsystem fired but the engine never engaged.
**Honesty caveat:** the spine is **structurally live but operationally under
validation** — per-dispatch confidence scoring and threshold-gating are *not*
implemented (every dispatch in the package fires), and playtest validation (59-8)
is still backlog.

## Key ADRs for this repo

| Domain | ADRs |
|--------|------|
| Core architecture | **101 (Anthropic SDK as narrator backend — supersedes 001)**, 001 (Claude CLI only — *superseded by 101*), 002 (SOUL principles), 005 (background-first), 006 (graceful degradation) |
| Genre packs | 003 (pack architecture), 004 (lazy binding) |
| Prompt engineering | 008 (three-tier taxonomy), 009 (attention-aware zones), 066 (persistent Opus sessions, Full/Delta tier — *superseded by 098*) |
| Agent system | 011 (JSON patches), 012 (session mgmt), 057 (narrator-crunch separation), 059 (monster manual server-side pregen), 067 (unified narrator agent — supersedes 010), **098 (stateless narrator turns — supersedes 066)**, **102 (tool-use protocol for structured output — supersedes 039)**, 113 (intent router — mechanical-engagement spine, *live/partial*) |
| Characters | 007 (unified model), 014 (diamonds/coal), 015 (builder FSM), 016 (three-mode chargen), 080 (unified narrative weight) |
| Encounters | 033 (confrontation engine — `ruleset/native.py`), 077 (dogfight subsystem), 078 (edge/composure combat), 093 (confrontation difficulty calibration), 114 (ablative HP substrate — `creature_core.py`, *partial: Part 1 live*) |
| World / NPCs | 018 (trope engine), 020 (NPC disposition), 022 (world maturity), 042 (OCEAN evolution), 055 (room graph navigation), 091 (culture-corpus Markov naming) |
| Progression | 021 (four-track), 052 (narrative axis), 081 (advancement effect variants — deferred), 095 (class mechanical surface) |
| Narrative pacing | 024 (dual-track tension), 025 (pacing detection), 050 (image pacing throttle), 051 (two-tier turn counter — see DRIFT) |
| Session persistence | 023 (state + recap), **115 (persistence substrate migration — SQLite-per-session → PostgreSQL, complete)** |
| Protocol | 026 (client state mirror), 027 (reactive state messaging), 074 (dice resolution protocol), 076 (narration protocol collapse post-TTS) |
| Multiplayer | 028 (perception rewriter — *superseded by 104*), 036 (multiplayer turn coordination), 037 (shared/per-player state split), 053 (scenario system), 104 (perception filtering at the tool layer), 105 (broadcast-layer perception firewall) |
| Transport / IPC | 035 (Unix socket IPC for Python sidecar), 038 (WebSocket transport), 046 (GPU memory budget), 047 (prompt injection sanitization) |
| Telemetry | 031 (game watcher semantic telemetry), 058 (Claude subprocess OTEL passthrough — *superseded by 103*), 090 (OTEL dashboard restoration), 103 (native OTEL via tool registry) |
| Media | 048 (lore RAG store, cross-process embedding), 050 (image pacing throttle), 086 (image-composition taxonomy) |
| Tooling / harness | 092 (scene harness HTTP endpoint — dev-gated) |
| Project lifecycle | 082 (port back to Python), 085 (tracker hygiene during port), 087 (post-port subsystem restoration) |
| Locations / dungeons | 055 (room graph navigation — the new `MAP_UPDATE` shape lives here; ADR-019 cartography `MAP_UPDATE` was deleted in the port), 096 (cavern renderer revival), 106 (runtime procedural Jaquaysed megadungeon — `beneath_sunden`), 109 (persistent location descriptions + mechanical manifest) |
| Multiplayer / OOC | 107 (non-turn-consuming out-of-band aside channel for OOC table-talk), 108 (MP item attribution — deferred) |
| Narrator tuning | 110 (snapshot slimming — deferred), 111 (recency-zone guardrails into tool descriptions — deferred), 112 (genre prose cache promotion — deferred) |

For the full ADR index see `orc-quest/docs/adr/README.md`. Drift notes: `orc-quest/docs/adr/DRIFT.md`. Superseded: `orc-quest/docs/adr/SUPERSEDED.md`.

## Save files

Saves live in a single PostgreSQL database (ADR-115), one `sessions` row per
genre/world session keyed by `session_slug` — not per-file SQLite, and not in
the repo. Connect via `SIDEQUEST_DATABASE_URL` (`SIDEQUEST_TEST_DATABASE_URL`
for tests); provision locally with `just pg-up`. The SQLite-per-session store
(`SqliteStore`/`SAVE_WRITE_LOCK`/WAL tuning) is retired; SQLite survives only as
a read-only import *source* via `python -m sidequest.game.importer`
(`sidequest/game/importer.py`). See
`orc-quest/.pennyfarthing/guides/save-management.md`. Saves are durable by
default — never reap save-referenced artifacts (portraits, audio) on a timer.

## Spoiler Protection

- **Fully spoilable:** `mutant_wasteland/flickering_reach` only
- **Fully unspoiled:** Everything else

## Git Workflow

- Branch strategy: gitflow
- Default branch: develop
- Feature branches: `feat/{description}`
- PRs target: develop
