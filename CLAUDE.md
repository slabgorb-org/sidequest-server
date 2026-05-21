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
├── handlers/         # Per-message-type dispatch handlers
├── agents/           # Anthropic SDK narrator (default) + claude -p/Ollama opt-in backends
├── game/             # ~70 modules — state, combat, chase, NPCs, OCEAN, lore, etc.
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

## Key ADRs for this repo

| Domain | ADRs |
|--------|------|
| Core architecture | **101 (Anthropic SDK as narrator backend — supersedes 001)**, 001 (Claude CLI only — *superseded by 101*), 002 (SOUL principles), 005 (background-first), 006 (graceful degradation) |
| Genre packs | 003 (pack architecture), 004 (lazy binding) |
| Prompt engineering | 008 (three-tier taxonomy), 009 (attention-aware zones), 066 (persistent Opus sessions, Full/Delta tier — *superseded by 098*) |
| Agent system | 011 (JSON patches), 012 (session mgmt), 057 (narrator-crunch separation), 059 (monster manual server-side pregen), 067 (unified narrator agent — supersedes 010), **098 (stateless narrator turns — supersedes 066)**, **102 (tool-use protocol for structured output — supersedes 039)** |
| Characters | 007 (unified model), 014 (diamonds/coal), 015 (builder FSM), 016 (three-mode chargen), 080 (unified narrative weight) |
| Encounters | 033 (confrontation engine), 077 (dogfight subsystem), 078 (edge/composure combat), 093 (confrontation difficulty calibration) |
| World / NPCs | 018 (trope engine), 020 (NPC disposition), 022 (world maturity), 042 (OCEAN evolution), 055 (room graph navigation), 091 (culture-corpus Markov naming) |
| Progression | 021 (four-track), 052 (narrative axis), 081 (advancement effect variants — deferred), 095 (class mechanical surface) |
| Narrative pacing | 024 (dual-track tension), 025 (pacing detection), 050 (image pacing throttle), 051 (two-tier turn counter — see DRIFT) |
| Session persistence | 023 (state + recap) |
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

SQLite databases at `~/.sidequest/saves/<genre>_<world>.db`, one per session.
Not in the repo. See `orc-quest/.pennyfarthing/guides/save-management.md` for
cleanup, inspection, and migration. Saves are durable by default — never reap
save-referenced artifacts (portraits, audio) on a timer.

## Spoiler Protection

- **Fully spoilable:** `mutant_wasteland/flickering_reach` only
- **Fully unspoiled:** Everything else

## Git Workflow

- Branch strategy: gitflow
- Default branch: develop
- Feature branches: `feat/{description}`
- PRs target: develop
