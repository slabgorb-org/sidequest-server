"""FastAPI application entry point for sidequest-server.

Phase 1: /health + /ws WebSocket endpoint + REST endpoints.
Dependency-injected: ClaudeClient factory, genre pack search paths, save dir
are all configurable for tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sidequest.agents.claude_client import LlmClient
from sidequest.agents.llm_factory import build_llm_client
from sidequest.agents.tooling_protocol import ToolingLlmClient
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS
from sidequest.server.reference_routes import create_reference_router
from sidequest.server.rest import create_rest_router
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry
from sidequest.server.watcher import (
    WatcherSpanProcessor,
    watcher_endpoint,
    watcher_hub,
)
from sidequest.server.websocket import ws_endpoint
from sidequest.telemetry.validator import Validator
from sidequest.telemetry.watcher_hub import publish_event

logger = logging.getLogger(__name__)


class WatcherEmitPayload(BaseModel):
    event_type: str
    fields: dict[str, Any]
    component: str = "daemon"


def _install_uvicorn_log_bridge() -> None:
    """Attach a StreamHandler to the ``sidequest`` logger tree so INFO
    lines surface in the uvicorn-driven log.

    Uvicorn installs its own dictConfig at serve time with
    ``disable_existing_loggers=True``, which disables any logger that
    isn't explicitly in its config. Attaching a handler directly on the
    ``sidequest`` tree keeps that tree alive through uvicorn's config
    swap without touching ``propagate`` — which would silently drop
    records from every caplog-based test later in the run.

    The previous implementation ran at module-import and set
    ``propagate=False``, which poisoned the logger tree for any test
    that later imported this module. The setup now runs only inside
    :func:`create_app`, and propagation stays on so tests using
    ``caplog`` continue to capture records off the root logger.
    """
    sq = logging.getLogger("sidequest")
    sq.setLevel(logging.INFO)
    if not any(getattr(h, "_sidequest_bridge", False) for h in sq.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
        handler._sidequest_bridge = True  # type: ignore[attr-defined]
        sq.addHandler(handler)


def create_app(
    claude_client_factory: Callable[[], LlmClient | ToolingLlmClient] | None = None,
    genre_pack_search_paths: list[Path] | None = None,
    save_dir: Path | None = None,
    ui_dist: Path | None = None,
    fixtures_dir: Path | None = None,
) -> FastAPI:
    """Construct the FastAPI application.

    Args:
        claude_client_factory: Factory that returns a LlmClient client.
            Defaults to ``build_llm_client`` (honours ``SIDEQUEST_LLM_BACKEND``).
        genre_pack_search_paths: Ordered list of directories to search for
            genre packs. Defaults to DEFAULT_GENRE_PACK_SEARCH_PATHS.
        save_dir: Root directory for SQLite save files.
            Defaults to ``~/.sidequest/saves``.
        ui_dist: Built UI dist directory (Vite ``dist/``) to serve at the
            root path with SPA fallback. Defaults to ``SIDEQUEST_UI_DIST``
            env. When unset OR pointing at a missing directory, the UI
            mount is skipped — local Vite dev (5173) handles serving;
            the tunneled production-style path requires this to be set.
        fixtures_dir: Directory containing scene harness fixture YAMLs.
            Defaults to ``scenarios/fixtures`` relative to cwd.
    """
    resolved_save_dir: Path = save_dir or (Path.home() / ".sidequest" / "saves")
    resolved_search_paths: list[Path] = (
        genre_pack_search_paths
        if genre_pack_search_paths is not None
        else DEFAULT_GENRE_PACK_SEARCH_PATHS
    )
    resolved_client_factory: Callable[[], LlmClient | ToolingLlmClient] = (
        claude_client_factory if claude_client_factory is not None else build_llm_client
    )

    _install_uvicorn_log_bridge()

    app = FastAPI(
        title="sidequest-server",
        description="SideQuest Python API server (ADR-082 port target)",
        version="0.1.0",
    )

    # CORS — dev UI runs on Vite (5173) and fetches API routes cross-origin.
    # Without this, dashboard polls hit `/api/debug/state` and friends from
    # 5173 → 8765 and the browser blocks with "No Access-Control-Allow-Origin",
    # flooding the console. Localhost-only by default; keeps prod safe because
    # the server isn't exposed beyond localhost anyway.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "https://sidequest.slabgorb.com",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store DI config on app.state so REST handlers can access it via Request
    app.state.claude_client_factory = resolved_client_factory
    app.state.genre_pack_search_paths = resolved_search_paths
    app.state.save_dir = resolved_save_dir
    app.state.room_registry = RoomRegistry()

    # --- Watcher hub — OTEL span broadcast for the GM dashboard. ---
    # The hub is a module-level singleton in `sidequest.server.watcher` so
    # subsystem code (session_handler, orchestrator) can publish semantic
    # events without threading a reference through every constructor.
    app.state.watcher_hub = watcher_hub
    app.state.validator = Validator()

    @app.on_event("startup")
    async def _start_validator() -> None:
        await app.state.validator.start()
        logger.info("validator.startup_wired")

    @app.on_event("shutdown")
    async def _stop_validator() -> None:
        v = getattr(app.state, "validator", None)
        if v is not None:
            await v.shutdown(grace_seconds=2.0)
            logger.info("validator.shutdown_wired")

    @app.on_event("startup")
    async def _wire_watcher() -> None:
        import asyncio

        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        from sidequest.telemetry.setup import init_tracer

        watcher_hub.bind_loop(asyncio.get_running_loop())

        # Ensure the global tracer provider is a real SDK TracerProvider
        # (not the default proxy) so add_span_processor is available.
        init_tracer()

        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            logger.warning(
                "watcher.span_processor_skipped reason=tracer_provider_is_%s",
                type(provider).__name__,
            )
            return

        # Idempotent registration. Under ``uvicorn --reload`` the startup
        # handler runs on every hot-reload, and the SDK's
        # ``add_span_processor`` happily stacks duplicates. Before this
        # guard, a 20-minute playtest session ended up with 30+
        # ``WatcherSpanProcessor`` instances, each pushing every span to
        # a (potentially stale) hub — the exact symptom on
        # 2026-04-23. We walk the provider's processor chain and skip
        # registration if one of ours is already wired up.
        existing = getattr(provider, "_active_span_processor", None)
        processors = getattr(existing, "_span_processors", ()) if existing else ()
        already_wired = any(isinstance(p, WatcherSpanProcessor) for p in processors)
        if already_wired:
            logger.info(
                "watcher.span_processor_already_registered count=%d",
                sum(1 for p in processors if isinstance(p, WatcherSpanProcessor)),
            )
            return

        provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
        logger.info("watcher.span_processor_registered")

    # Story 45-31: spin up the daemon heartbeat listener so the
    # process-wide DaemonStateMirror starts populating from the
    # daemon's heartbeat stream. Without this, the dispatcher's
    # UNRESPONSIVE-fallback branch is unreachable and the Felix
    # anti-13-minute-silence contract never engages in production.
    @app.on_event("startup")
    async def _start_heartbeat_listener() -> None:
        from sidequest.daemon_client import DaemonClient

        client = DaemonClient()
        task = asyncio.create_task(
            client.heartbeat_listener(),
            name="daemon-heartbeat-listener",
        )
        app.state.heartbeat_listener_task = task
        logger.info("daemon.heartbeat_listener_started socket=%s", client.socket_path)

    @app.on_event("shutdown")
    async def _stop_heartbeat_listener() -> None:
        task = getattr(app.state, "heartbeat_listener_task", None)
        if task is not None and not task.done():
            task.cancel()
            # ``await task`` after ``cancel()`` raises
            # ``asyncio.CancelledError`` (a ``BaseException`` in
            # Python 3.8+, so a plain ``suppress(Exception)`` misses
            # it). The task body itself may raise OSError or
            # RuntimeError if the underlying socket was torn down
            # mid-cycle. Suppress both classes explicitly to keep
            # shutdown clean — teardown-time exceptions must not
            # leak into the lifespan exit.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            logger.info("daemon.heartbeat_listener_stopped")

    # --- Postgres pool lifecycle (ADR-115 F2) ---
    # Open and verify the process-global pool at boot so a dead/unreachable
    # Postgres fails LOUD at startup rather than lazily on the first save
    # mid-session (No Silent Fallbacks). Close + discard it on shutdown so the
    # connections drain cleanly and a uvicorn --reload starts from a fresh pool.
    @app.on_event("startup")
    async def _open_db_pool() -> None:
        from sidequest.game import db_pool
        from sidequest.game.db_schema_check import assert_schema_at_head

        pool = db_pool.get_pool()
        pool.wait(timeout=10.0)  # fail loud if Postgres is unreachable at boot
        # Reachable is not enough: a schema stamped behind alembic head boots
        # fine and only explodes mid-turn on the first write to an unmigrated
        # table (playtest #G4). Extend the ADR-115 fail-loud contract to assert
        # the schema is at head before declaring the pool wired.
        assert_schema_at_head()
        logger.info("db_pool.startup_wired name=%s", pool.name)

    @app.on_event("shutdown")
    async def _close_db_pool() -> None:
        from sidequest.game import db_pool

        db_pool.close_pool()
        logger.info("db_pool.shutdown_wired")

    # --- /health ---
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- /internal/watcher/emit — daemon → server watcher bridge (ADR-031). ---
    # The daemon cannot import sidequest.telemetry.watcher_hub directly
    # (cross-process boundary). It POSTs here and we forward to publish_event.
    @app.post("/internal/watcher/emit", status_code=204)
    async def watcher_emit(payload: WatcherEmitPayload) -> None:
        publish_event(
            event_type=payload.event_type,
            fields=payload.fields,
            component=payload.component,
        )

    # --- /ws WebSocket endpoint ---
    @app.websocket("/ws")
    async def websocket_game(websocket: WebSocket) -> None:
        handler = WebSocketSessionHandler(
            claude_client_factory=app.state.claude_client_factory,
            genre_pack_search_paths=app.state.genre_pack_search_paths,
            save_dir=app.state.save_dir,
            validator=app.state.validator,
        )
        await ws_endpoint(websocket, handler)

    # --- /ws/watcher WebSocket endpoint — OTEL span stream to GM dashboard. ---
    @app.websocket("/ws/watcher")
    async def websocket_watcher(websocket: WebSocket) -> None:
        await watcher_endpoint(websocket, watcher_hub)

    # --- REST routes ---
    rest_router = create_rest_router()
    app.include_router(rest_router)

    # --- Reference pages (/reference/rules/*, /reference/lore/*) ---
    app.include_router(create_reference_router())

    # --- Scene-harness routes (ADR-092). ---
    # Always registered — Cloudflare Zero Trust gates access at the tunnel
    # layer; the former DEV_SCENES env var added zero security value.
    from sidequest.server.scene_harness_router import create_scene_harness_router

    resolved_fixtures_dir = fixtures_dir or Path("scenarios/fixtures")
    app.state.fixtures_dir = resolved_fixtures_dir
    app.include_router(create_scene_harness_router())
    logger.info(
        "scene_harness.route_registered fixtures_dir=%s",
        resolved_fixtures_dir,
    )

    # --- Chassis interior map (Ship tab) ---
    from sidequest.interior.dispatch import interior_router

    app.include_router(interior_router)

    # --- Static /genre/* mount — serve genre pack assets (POI images, portraits, etc.) ---
    # URL /genre/<genre>/worlds/<world>/assets/poi/<file> → first-matching genre_packs dir.
    # When no genre_packs dir exists (unit-test configurations that pass
    # a nonexistent path), skip the mount entirely — asset requests will
    # 404 loudly, which is correct signal, while the rest of the app
    # (including /api/genres returning {}) stays usable.
    genre_packs_dir: Path | None = next(
        (p for p in resolved_search_paths if p.exists() and p.is_dir()), None
    )
    if genre_packs_dir is not None:
        app.mount("/genre", StaticFiles(directory=str(genre_packs_dir)), name="genre_assets")
    else:
        logger.warning(
            "genre_assets.mount_skipped search_paths=%s",
            [str(p) for p in resolved_search_paths],
        )

    # --- Static /renders/* mount — serve daemon-generated images. ---
    # Source-of-truth precedence:
    #   1. SIDEQUEST_OUTPUT_DIR env (explicit override, prod or shared dev).
    #   2. ~/.sidequest/daemon-output-dir handshake file written by the
    #      daemon at startup (covers the dev-default case where the daemon
    #      picks a random tmpdir and the server can't discover it through
    #      env). Resolves the playtest 2026-04-25 [P1] regression where
    #      every render landed in the daemon's tmpdir and the UI 404'd
    #      because no /renders mount existed.
    #   3. No mount — renders unreachable; logged loudly.
    # Once resolved, propagate into the env so `_render_url_from_path`
    # (called per render in session_handler) sees the same value.
    import os as _os

    render_root = _os.environ.get("SIDEQUEST_OUTPUT_DIR")
    handshake_source = "env"
    if not render_root:
        handshake_path = Path.home() / ".sidequest" / "daemon-output-dir"
        if handshake_path.is_file():
            try:
                render_root = handshake_path.read_text().strip()
                handshake_source = "handshake"
                _os.environ["SIDEQUEST_OUTPUT_DIR"] = render_root
            except OSError as exc:
                logger.warning(
                    "render_assets.handshake_read_failed path=%s error=%s",
                    handshake_path,
                    exc,
                )
    if render_root:
        render_dir = Path(render_root)
        render_dir.mkdir(parents=True, exist_ok=True)
        app.mount(
            "/renders",
            StaticFiles(directory=str(render_dir)),
            name="render_assets",
        )
        logger.info(
            "render_assets.mount_registered dir=%s source=%s",
            render_dir,
            handshake_source,
        )
        # Self-healing across daemon restarts: when the daemon dies and
        # respawns, the new daemon picks a fresh ``tempfile.mkdtemp(prefix=
        # "sq-daemon-")`` and the prior ``sq-daemon-XXX`` dir survives on
        # disk until the OS reaps it. URLs the UI cached from prior
        # renders point into those orphan dirs and 404 against the current
        # mount alone. Pre-register every sibling ``sq-daemon-*`` dir so
        # cached URLs continue to resolve as long as the temp tree exists.
        # Idempotent inside ``register_root``; failures are logged, never
        # raised — orphan scan must not crash startup.
        try:
            from sidequest.server.render_mounts import register_daemon_temp_orphans

            register_daemon_temp_orphans(app, render_dir.parent)
        except Exception as exc:
            logger.warning(
                "render_assets.orphan_scan_failed parent=%s error=%s",
                render_dir.parent,
                exc,
            )
    else:
        logger.warning(
            "render_assets.mount_skipped reason=no_env_no_handshake — "
            "every render will fall through unrewritten and the UI will 404"
        )

    # --- Self-healing render-mount middleware (S4-BUG fix). ---
    # Forensic 404 publisher — when a /renders/* URL still 404s after
    # the on-render mount logic ran, surface it in the GM panel so the
    # next regression isn't silent. De-duplicated per URL inside the
    # registry to keep the dashboard quiet.
    from sidequest.server import render_mounts as _render_mounts

    _render_mounts.set_active_app(app)

    @app.middleware("http")
    async def _render_404_watcher(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if response.status_code == 404 and request.url.path.startswith("/renders/"):
            _render_mounts.publish_url_404(request.url.path)
        return response

    # --- UI dist + SPA fallback (must register LAST so it only catches
    # paths no other route claims). ---
    # Production / tunnel path: serve the built React UI under ``/`` with
    # a catch-all that returns ``index.html`` for any unmatched GET so
    # client-side router paths (``/play/<slug>``, ``/lobby``, etc.)
    # resolve. Replaces the prior ``StaticFiles(html=True)`` mount from
    # PR #179 — that flag only handles the directory-root case; bookmarks,
    # shared session URLs, and direct deep-links to client-side routes
    # require a full SPA fallback (playtest 2026-05-03 [BUG] tunnel
    # deep-links 404).
    #
    # Source-of-truth precedence:
    #   1. ``ui_dist`` constructor arg (test injection).
    #   2. ``SIDEQUEST_UI_DIST`` env (prod / tunnel deployments).
    #   3. Skip the mount entirely (Vite dev on :5173 handles serving).
    resolved_ui_dist: Path | None = ui_dist
    if resolved_ui_dist is None:
        env_ui = _os.environ.get("SIDEQUEST_UI_DIST")
        if env_ui:
            resolved_ui_dist = Path(env_ui)
    if resolved_ui_dist is not None and resolved_ui_dist.is_dir():
        _install_spa_fallback(app, resolved_ui_dist)
    elif resolved_ui_dist is not None:
        # Loud-fail: env set but dir missing. No silent fallback.
        logger.warning(
            "ui_dist.mount_skipped reason=missing_directory path=%s",
            resolved_ui_dist,
        )

    return app


def _install_spa_fallback(app: FastAPI, ui_dist: Path) -> None:
    """Mount ``ui_dist/assets`` as static + register a GET catch-all that
    serves real files from ``ui_dist`` when present, falling through to
    ``index.html`` for client-side router paths.

    Registration order: this runs LAST in ``create_app``, so all other
    routes (``/health``, ``/api/*``, ``/genre/*``, ``/renders/*``,
    ``/dashboard``, ``/ws``) win on prefix match. The catch-all only
    fires for paths that nothing else claimed.
    """
    from fastapi.responses import FileResponse, Response

    index_path = ui_dist / "index.html"
    assets_dir = ui_dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="ui_assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Response:
        # Try a literal file under ui_dist first (covers favicon.ico,
        # manifest.json, robots.txt, and any other root-level static).
        if full_path:
            candidate = (ui_dist / full_path).resolve()
            try:
                # is_relative_to guards against ``..`` escaping the dist
                # root even though Starlette normalizes the URL upstream.
                candidate.relative_to(ui_dist.resolve())
            except ValueError:
                pass
            else:
                if candidate.is_file():
                    return FileResponse(candidate)
        # Otherwise serve the SPA shell so React Router can take over.
        if index_path.is_file():
            return FileResponse(index_path)
        # ui_dist exists but no index.html — caller misconfigured the
        # build output. 404 loudly rather than serve a blank.
        return Response(
            content="ui_dist missing index.html",
            status_code=404,
            media_type="text/plain",
        )

    logger.info("ui_dist.mount_registered path=%s", ui_dist)


def main() -> None:
    """Entry point for `sidequest-server` CLI script."""
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
