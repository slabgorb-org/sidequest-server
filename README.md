# sidequest-server

Python FastAPI game engine for SideQuest — the live backend (port 8765). Hosts the WebSocket transport, narrator orchestration, genre pack runtime, and game state.

See [ADR-082](../docs/adr/082-port-api-rust-to-python.md) for the port history; the Rust prototype `sidequest-api` is preserved read-only at <https://github.com/slabgorb/sidequest-api> for archaeology only.

## Quick start

```bash
uv sync                           # Install deps
uv run pytest -v                  # Tests
uv run ruff check .               # Lint
uv run uvicorn sidequest.server.app:app --reload --port 8765   # Boot
```

From the orchestrator root: `just server`, `just server-test`, `just server-check`.

## Stack

- **FastAPI** + **uvicorn** — HTTP, WebSocket, static file serving
- **pydantic v2** — Typed protocol (`GameMessage` discriminated union) and genre pack models
- **PostgreSQL** (`psycopg3` + `psycopg_pool`) — Save persistence in one logical database, sessions keyed by `session_slug` (ADR-115). Connection URL via `SIDEQUEST_DATABASE_URL`; schema managed by Alembic. Replaces the retired SQLite-per-session store (`SqliteStore`/`SAVE_WRITE_LOCK`/WAL tuning, all deleted)
- **PyYAML** — Genre pack loader (read-only at runtime)
- **OpenTelemetry** — Span emission for the GM dashboard (ADR-090; native OTEL via the tool registry per ADR-103 supersedes the legacy `claude -p` subprocess passthrough of ADR-058)
- **websockets** — Watcher channel transport
- **uv** — Dependency management; `pyproject.toml` is the source of truth
- **Python 3.12+**

Narrator LLM calls go through the **Anthropic Python SDK** by default (`anthropic>=0.40`, ADR-101, supersedes ADR-001) — prompt caching, native tool-use, per-call model routing. `SIDEQUEST_LLM_BACKEND` selects the backend (default `anthropic_sdk`); `claude -p` (`claude_client.py`) and Ollama remain opt-in non-default backends, and `claude -p` still serves some non-narrator jobs. Media generation goes over a Unix socket to `sidequest-daemon` (ADR-035).

## Package layout

```
sidequest/
├── protocol/         # GameMessage, typed payloads (pydantic v2)
├── server/           # FastAPI app, WebSocket, dispatch, sessions, watcher
├── handlers/         # Per-message-type dispatch handlers
├── agents/           # Anthropic SDK narrator (default) + claude -p/Ollama opt-in, auxiliaries
├── game/             # State, characters, encounters, tropes, turns, persistence (~70 modules)
│                     #   game/ruleset/ — pluggable SRD modules: native + Without Number
│                     #   family (without_number base + swn/wwn/cwn/awn, ADR-117/142/143)
├── dungeon/          # Runtime procedural Jaquaysed megadungeon (ADR-106)
├── mutation/         # AWN mutation system — acquire/use ops, stocks (ADR-102)
├── genre/            # YAML loader, layered genre/world pack models
├── audio/            # Server-side music + SFX coordination
├── media/            # Image generation orchestration (daemon client wrapper)
├── magic/            # Magic system mechanics
├── interior/         # Room / interior state
├── orbital/          # Orbital / space-scene mechanics
├── corpus/           # Conlang corpus + Markov naming (ADR-091)
├── renderer/         # Render scheduling + throttle (ADR-050)
├── daemon_client/    # Unix-socket client for the media daemon
├── telemetry/        # OTEL span definitions and watcher hooks
└── cli/              # Standalone CLIs (see below)
```

The package composition mirrors the Rust crate layout 1:1 (per ADR-082) so historical features can be traced by path. Post-port refactoring is a separate decision.

## CLIs

Entry points under `sidequest/cli/`:

| CLI | Purpose |
|-----|---------|
| `encountergen` | Pre-generate encounter rosters into the Monster Manual (ADR-059) |
| `loadoutgen` | Generate loadout tables |
| `namegen` | Markov-generated names from culture corpora (ADR-091) |
| `validate` | Validate a genre pack against schema |
| `weathergen` | Generate weather tables for narrator grounding |
| `cookbook_ingest` | Ingest SRD/cookbook text into corpora |
| `corpusmine` | Mine word lists from text |
| `corpuslabel` | Annotate corpus entries |
| `corpusdiff` | Diff two corpora |

Run via `uv run python -m sidequest.cli.<name>`. Only `sidequest-server` is
registered as a console script in `pyproject.toml`; these CLIs are invoked as
modules, not installed entry points.

## Endpoints

- **WebSocket `/ws`** — Primary game transport. `GameMessage` JSON in/out, discriminated on `type`. See [`docs/api-contract.md`](../docs/api-contract.md).
- **WebSocket `/ws/watcher`** — OTEL telemetry stream for the GM dashboard.
- **REST** — Small surface: `/api/genres`, save list, character list, scene harness (ADR-092, dev-gated).
- **Static** — `/renders/*` for daemon-produced images, `/dashboard` for the GM UI.

## Session model

- One WebSocket connection = one asyncio task owning a `Session`.
- Solo: one session, one orchestrator, no contention.
- Multiplayer: sessions share a `SessionRoom` keyed by `genre:world` behind `asyncio.Lock`, with `TurnBarrier` for coordinated turn resolution (ADR-036, ADR-037).
- Three turn modes exist in code: `FREE_PLAY`, `STRUCTURED`, `CINEMATIC`. **Cinematic is the live default**; FREE_PLAY is available; STRUCTURED is dead code retained for future activation. Peer action text is visible during the wait phase (collaborative default per ADR-036 amendment 2026-05-03); see `handlers/action_reveal.py` for the `ACTION_REVEAL` fan-out (live teammate typing).

## Key ADRs

- **ADR-098** Stateless narrator turns — the narrator is invoked fresh each turn with a bounded prompt (no `--resume`, no `narrator_session_id`); on the default `anthropic_sdk` backend (ADR-101) this is a single SDK call per turn. Supersedes ADR-066's persistent-session model and the §8 warm-reboot recovery path. Streaming narration is opt-in and **default-off** (`SIDEQUEST_NARRATOR_STREAMING=1` enables it and routes to the legacy `claude -p` path).
- **ADR-067** Unified narrator agent — one narrator handles exploration, dialogue, combat, and chase narration. Auxiliary subsystem agents (chassis_voice, distinctive_detail, npc_agency, reflect_absence) run off the critical path.
- **ADR-059** Monster Manual — NPCs and encounters pre-generated server-side via CLI and injected into the narrator's `<game_state>` block. Narrator-side tool calling was abandoned (currently drift — ADR-087 P0 RESTORE).
- **ADR-038** WebSocket transport — reader/writer task split, broadcast channels.
- **ADR-036** Multiplayer turn coordination — barrier + CAS-guarded dispatcher; two amendments (2026-05-03 action-visibility model; 2026-05-09 sealed-letter disambiguation) document the collaborative-visibility default.
- **ADR-005** Background-first — only text narration is on the critical path; media, deltas, trope tick, lore accumulation run async.

See [`docs/architecture.md`](../docs/architecture.md) for the full system design and [`docs/adr/`](../docs/adr/) for all decisions.

## Game state and saves

- **Save format:** Rows in a single PostgreSQL database (ADR-115), one `sessions` row per genre/world session keyed by `session_slug`. Connect via `SIDEQUEST_DATABASE_URL`; tests use `SIDEQUEST_TEST_DATABASE_URL`. Provision locally with `just pg-up` (Homebrew `postgresql@18`). Legacy SQLite saves import via `python -m sidequest.game.importer` (`sidequest/game/importer.py`).
- **Narrative log:** Append-only.
- **KnownFacts:** Accumulate across turns with provenance.
- DB calls borrow a pooled connection; async handlers offload via `anyio.to_thread`.

## Testing

```bash
uv run pytest -v                  # Full suite
uv run pytest tests/test_X.py     # One file
uv run pytest -k "name pattern"   # Filter
```

`pytest-asyncio` is configured for `asyncio_mode = "auto"`. Tests run against in-memory state — no daemon required for the default suite.

## Branching

Gitflow. `develop` is the integration branch. `main` tracks releases. PRs target `develop`.

## Related repos

- [sidequest](https://github.com/slabgorb-org/sidequest) — Orchestrator, ADRs, sprint tracking
- [sidequest-ui](https://github.com/slabgorb-org/sidequest-ui) — React client
- [sidequest-daemon](https://github.com/slabgorb-org/sidequest-daemon) — Python media services (Z-Image, ACE-Step)
- [sidequest-content](https://github.com/slabgorb-org/sidequest-content) — Genre packs (single source of truth)
- [sidequest-composer](https://github.com/slabgorb-org/sidequest-composer) — Notation → rights-free audio (offline tool)
- [sidequest-understudy](https://github.com/slabgorb-org/sidequest-understudy) — Naive simulated-player playtest client
