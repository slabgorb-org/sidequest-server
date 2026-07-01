"""REST API endpoints for sidequest-server.

Endpoints:
  GET /api/genres    — list available genre packs with world metadata (lobby picker)
  POST /api/games    — mint a new game slug (slug-keyed save model, MP-03)
  GET /api/sessions  — list active sessions (Phase 1: always empty; multiplayer is Phase N)
  GET /api/debug/state — GM dashboard projection over persisted sessions

The legacy ``/api/saves/*`` triple (list/create/delete) and the matching
``(genre, world, player_name)``-tuple save-path helper were removed in
Story 45-26 once UI confirmed exclusive use of ``game_slug``.
"""

from __future__ import annotations

import logging
from datetime import date as _date_cls
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from sidequest.foundation.asset_urls import resolve_asset_url, resolve_player_portrait_url
from sidequest.game.game_slug import generate_slug
from sidequest.game.persistence import (
    GameMode,
)
from sidequest.genre.loader import (
    DEFAULT_GENRE_PACK_SEARCH_PATHS,
    load_genre_pack,
    load_genre_pack_cached,
)
from sidequest.genre.models.pack import picker_portrait_slug
from sidequest.interior.render import render_interior_svg
from sidequest.server.state_projection import project_session_state_view
from sidequest.telemetry.spans.interior import emit_interior_render

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class WorldMeta(BaseModel):
    """Lobby metadata for a single world. Mirrors Rust WorldResponse."""

    slug: str
    name: str
    description: str
    era: str | None = None
    setting: str | None = None
    inspirations: list[str] = []
    axis_snapshot: dict[str, float] = {}
    hero_image: str | None = None
    # Stable location-capability signal for the lobby/GameBoard. Derived from
    # the world's sibling cartography.yaml: the file's ``navigation_mode``
    # (``region`` by CartographyConfig default), or ``None`` when the world
    # has no cartography.yaml at all (no location capability). The UI gates
    # the Location tab on this so the tab is stable per session rather than
    # blinking with transient LOCATION_DESCRIPTION traffic.
    navigation_mode: str | None = None


class GenreMeta(BaseModel):
    """Lobby metadata for a genre. Mirrors Rust GenreResponse."""

    name: str
    description: str
    worlds: list[WorldMeta] = []


class CreateGameRequest(BaseModel):
    genre_slug: str
    world_slug: str
    mode: GameMode  # pydantic rejects unknown enum values with 422
    # Lobby companions to sidequest-ui develop 1436ebd. Both optional so older
    # clients (and curl-based smoke tests) keep working without a body change.
    player_name: str | None = None
    # Deprecated (2026-06-13): slugs are now unique per create, so there is no
    # same-slug collision to disambiguate. Accepted for request-shape
    # back-compat with older clients; no longer consulted. See create_game.
    force_new: bool = False


class GameResponse(BaseModel):
    slug: str
    mode: GameMode
    genre_slug: str
    world_slug: str
    resumed: bool
    # Echoed back so the lobby can display the typed name without a second
    # round-trip; ``None`` when the request did not send one.
    player_name: str | None = None
    # Orbital capability — True when the bound world ships an ``orbits.yaml``
    # (the same opt-in ``bind_world`` reads). The Map tab gates the
    # OrbitalChartView on this server-announced fact instead of a per-world
    # frontend hardcode (sq-playtest 2026-06-07: perseus_cloud shipped
    # orbits.yaml via content#383 + server#728 but the UI allowlist still
    # only contained coyote_star, so the orrery was unreachable).
    orbital: bool = False
    # Retained for response-shape back-compat with the lobby (``useStartGame``
    # reads it defensively). Always empty now: as of the unique-slug change
    # (2026-06-13) ``POST /api/games`` always mints a fresh game, so a create
    # never resumes an existing table. Resuming/joining happens by opening the
    # exact ``/play/<slug>`` link, which loads the cast over the WebSocket.
    existing_characters: list[str] = []


def _world_has_orbits(request: Request, genre_slug: str, world_slug: str) -> bool:
    """True when ``<pack>/worlds/<world>/orbits.yaml`` exists.

    The same opt-in file ``bind_world`` hands to ``load_orbital_content`` —
    announced on ``GameResponse.orbital`` so the Map tab can gate the
    OrbitalChartView on server truth instead of a per-world UI hardcode.
    Existence only (no parse): a malformed orbits.yaml fails loud at bind,
    which is the correct place for schema enforcement.
    """
    search_paths: list[Path] = getattr(
        request.app.state,
        "genre_pack_search_paths",
        DEFAULT_GENRE_PACK_SEARCH_PATHS,
    )
    for sp in search_paths:
        if sp.exists() and sp.is_dir():
            return (sp / genre_slug / "worlds" / world_slug / "orbits.yaml").exists()
    return False


# ---------------------------------------------------------------------------
# Chassis interior SVG (Ship tab) — GET /api/chassis/{instance_id}/interior.
# Lifted here from sidequest.interior.dispatch (ADR-147, story 122-3) so the
# interior/ subsystem stays pure: HTTP lives in the server tier, the renderer
# (interior.render) stays a pure dependency the server imports downward.
#
# Walks the configured genre-pack search paths to find the chassis instance,
# looks up its chassis class, and renders the interior. The snapshot is empty
# for v1 — live session-bound rendering (PCs at their real ``current_room``,
# NPCs from the live snapshot) is a follow-on; the renderer's hardcoded NPC
# defaults plus the chassis-default-room fallback for PCs are enough for now.
# ---------------------------------------------------------------------------


def _find_chassis_instance(search_paths: list[Path], instance_id: str):
    """Return (chassis_class, chassis_instance_config, genre_slug, world_slug)
    or (None, None, None, None) if no instance with this id is authored.
    """
    for sp in search_paths:
        if not (sp.exists() and sp.is_dir()):
            continue
        for genre_dir in sorted(sp.iterdir()):
            if not genre_dir.is_dir():
                continue
            try:
                pack = load_genre_pack(genre_dir)
            except Exception as exc:
                logger.warning(
                    "interior: skipping pack %s (load failed: %s)",
                    genre_dir.name,
                    exc,
                )
                continue
            # Epic 94: chassis_classes is a world-tier surface (genre = rulebook
            # only). Read it world-first off each World; the old genre-tier
            # pack.chassis_classes is None for migrated packs.
            for world_slug, world in pack.worlds.items():
                if world.chassis_classes is None:
                    continue
                for inst_cfg in world.chassis_instances:
                    if inst_cfg.id == instance_id:
                        chassis_class = next(
                            (
                                c
                                for c in world.chassis_classes.classes
                                if c.id == inst_cfg.chassis_class_id
                            ),
                            None,
                        )
                        return chassis_class, inst_cfg, genre_dir.name, world_slug
    return None, None, None, None


class _EmptySnapshot:
    """Stand-in for the live game snapshot when the endpoint is hit
    outside of a session (e.g., direct curl). Matches the duck shape
    the renderer reads."""

    characters: list = []
    npcs: list = []


# ---------------------------------------------------------------------------
# Router factory (takes search_paths + save_dir from app.state)
# ---------------------------------------------------------------------------


def create_rest_router() -> APIRouter:
    """Create the REST API router.

    Handlers pull config from request.app.state:
      - app.state.genre_pack_search_paths: list[Path]
      - app.state.save_dir: Path
    """
    router = APIRouter()

    @router.get("/api/genres")
    async def list_genres(request: Request) -> dict[str, Any]:
        """List available genre packs with lobby-ready world metadata.

        Mirrors Rust list_genres() in sidequest-server/src/lib.rs.
        Returns { genre_slug: { name, description, worlds: [...] } }.
        Malformed packs are logged and skipped — one broken pack must not
        break the entire lobby.
        """
        search_paths: list[Path] = getattr(
            request.app.state,
            "genre_pack_search_paths",
            DEFAULT_GENRE_PACK_SEARCH_PATHS,
        )

        # Find first valid genre_packs directory
        packs_path: Path | None = None
        for sp in search_paths:
            if sp.exists() and sp.is_dir():
                packs_path = sp
                break

        if packs_path is None:
            logger.warning(
                "list_genres: no genre packs directory found in %s",
                [str(p) for p in search_paths],
            )
            return {}

        genres: dict[str, Any] = {}

        for entry in sorted(packs_path.iterdir()):
            if not entry.is_dir():
                continue
            genre_slug = entry.name

            pack_yaml_path = entry / "pack.yaml"
            if not pack_yaml_path.exists():
                continue

            # Parse pack.yaml for name + description
            try:
                raw = yaml.safe_load(pack_yaml_path.read_text(encoding="utf-8"))
                name = str(raw.get("name", genre_slug))
                description = str(raw.get("description", ""))
            except Exception as exc:
                logger.warning(
                    "list_genres: skipping '%s' — pack.yaml failed: %s",
                    genre_slug,
                    exc,
                )
                continue

            # Walk worlds/ subdirectory
            worlds_dir = entry / "worlds"
            worlds: list[dict[str, Any]] = []
            if worlds_dir.exists():
                for world_entry in sorted(worlds_dir.iterdir()):
                    # Skip symlinks — they exist as backwards-compat aliases
                    # for renamed world slugs (e.g. primetime → dungeon_survivor).
                    # Slug-based resume still resolves through them, but the
                    # lobby must not list the same world twice under both names.
                    if world_entry.is_symlink():
                        continue
                    if not world_entry.is_dir():
                        continue
                    world_slug = world_entry.name
                    world_yaml_path = world_entry / "world.yaml"
                    if not world_yaml_path.exists():
                        logger.warning(
                            "list_genres: skipping world '%s/%s' — world.yaml missing",
                            genre_slug,
                            world_slug,
                        )
                        continue

                    try:
                        wraw = yaml.safe_load(world_yaml_path.read_text(encoding="utf-8"))
                    except Exception as exc:
                        logger.warning(
                            "list_genres: skipping world '%s/%s' — world.yaml parse: %s",
                            genre_slug,
                            world_slug,
                            exc,
                        )
                        continue

                    # Honor the same draft skip the pack loader uses
                    # (_load_single_world returns None for draft: true). A draft
                    # world the lobby offers cannot actually load its content —
                    # the session would fall back silently to genre/sibling-world
                    # defaults (No-Silent-Fallbacks violation). Skip it loudly.
                    if wraw.get("draft"):
                        logger.info(
                            "list_genres: skipping draft world '%s/%s' — not offered "
                            "in lobby (draft: true; loader excludes it)",
                            genre_slug,
                            world_slug,
                        )
                        continue

                    wname = str(wraw.get("name", world_slug))
                    wdesc = str(wraw.get("description", ""))
                    wera = wraw.get("era")
                    wsetting = wraw.get("setting")
                    winsp = wraw.get("inspirations", [])
                    if not isinstance(winsp, list):
                        winsp = []
                    winsp = [str(i) for i in winsp]
                    waxis = wraw.get("axis_snapshot", {})
                    if not isinstance(waxis, dict):
                        waxis = {}

                    # Resolve cover_poi → hero_image URL via the asset_urls
                    # seam (default: https://cdn.slabgorb.com; "local" override
                    # rewrites to /genre/* against $SIDEQUEST_GENRE_PACKS).
                    # POI generator (scripts/render_common.py) emits .png only;
                    # the multi-extension probe was a Rust-port leftover.
                    cover_poi = wraw.get("cover_poi")
                    hero_image: str | None = None
                    if cover_poi:
                        rel_path = (
                            f"genre_packs/{genre_slug}/worlds/{world_slug}"
                            f"/assets/poi/{cover_poi}.png"
                        )
                        hero_image = resolve_asset_url(rel_path)
                    else:
                        logger.warning(
                            "list_genres: no cover_poi in world.yaml for %s/%s — "
                            "lobby preview will show placeholder",
                            genre_slug,
                            world_slug,
                        )

                    # Location capability — read the sibling cartography.yaml.
                    # Present file → its navigation_mode (default "region" per
                    # CartographyConfig). Absent file → None (no location
                    # capability). A malformed cartography.yaml is logged loudly
                    # and treated as no-capability rather than dropping the
                    # otherwise-playable world from the lobby.
                    navigation_mode: str | None = None
                    cart_yaml_path = world_entry / "cartography.yaml"
                    if cart_yaml_path.exists():
                        try:
                            craw = yaml.safe_load(cart_yaml_path.read_text(encoding="utf-8"))
                            navigation_mode = str((craw or {}).get("navigation_mode", "region"))
                        except Exception as exc:
                            logger.warning(
                                "list_genres: cartography.yaml parse failed for "
                                "%s/%s — Location tab disabled: %s",
                                genre_slug,
                                world_slug,
                                exc,
                            )

                    worlds.append(
                        {
                            "slug": world_slug,
                            "name": wname,
                            "description": wdesc,
                            "era": wera,
                            "setting": wsetting,
                            "inspirations": winsp,
                            "axis_snapshot": waxis,
                            "hero_image": hero_image,
                            "navigation_mode": navigation_mode,
                        }
                    )

            genres[genre_slug] = {
                "name": name,
                "description": description,
                "worlds": worlds,
            }

        return genres

    @router.get("/api/sessions")
    async def list_sessions(request: Request) -> dict[str, Any]:
        """List active sessions.

        Phase 1: single-player only — always returns empty sessions list.
        Phase N: multiplayer SharedGameSession sync.
        """
        return {"sessions": []}

    @router.get("/api/debug/state")
    async def debug_state(
        request: Request,
        session_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Enumerate persisted game sessions for the GM dashboard State tab.

        Enumerates slugs via ``PgForensicReader.list_saves()`` and loads each
        snapshot via ``PgSaveRepository.load()``, projecting each loaded
        :class:`GameSnapshot` onto the ``SessionStateView`` shape defined in
        ``sidequest-ui/src/types/watcher.ts``. Read-only; a slug that fails to
        load is skipped rather than failing the request.

        Results are sorted by ``last_activity_ts``, most-recently-touched
        first — so the dashboard's default "index 0" pick lands on the active
        session rather than an old save. Each view includes
        ``last_activity_ts`` (ms since epoch) so the UI can also pick
        explicitly.

        If ``session_key`` is provided, only that slug's view is returned
        (still as a list, to keep the wire shape stable). Missing slug →
        empty list, not a 404 — the dashboard treats this endpoint as
        lossy/best-effort.

        ADR-115 D7: sessions and snapshots are read from Postgres via
        ``PgForensicReader.list_saves()`` (slug enumeration) +
        ``PgSaveRepository.load()`` (snapshot). No SQLite save.db walk, no
        silent fallback — a missing/unknown slug yields the lossy-empty list.
        """
        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions
        from sidequest.game.pg.forensic import PgForensicReader
        from sidequest.game.pg.save_repository import PgSaveRepository

        pool = _db_pool.get_pool()
        reader = PgForensicReader(pool)

        views: list[dict[str, Any]] = []
        # Enumerate candidate slugs, optionally filtered to a single
        # session_key so the dashboard can target the active session
        # directly (playtest 2026-04-24 — the State tab defaulted to
        # index 0 which was the oldest save, not the active one).
        # list_saves already returns newest-first by last_activity_ts.
        if session_key is not None:
            save_rows = [r for r in reader.list_saves() if r["slug"] == session_key]
        else:
            save_rows = reader.list_saves()
        for save_row in save_rows:
            slug = save_row["slug"]
            # Per-slug resilience (restored in D7 review): one bad save — a
            # snapshot that load() can't deserialize — must not 500 the entire
            # State tab. Log loudly (No-Silent-Fallbacks: this is observable,
            # not a quiet alternative path) and skip just that slug.
            try:
                # Read-only projection: resolve the session_id and bind the
                # repository DIRECTLY. We must NOT route through
                # ``PgSaveRepository.for_slug``, whose ``ensure_session`` upserts
                # ``ON CONFLICT … SET last_played = now()`` and so bumps
                # ``last_activity_ts`` on every dashboard poll — floating idle
                # (e.g. test-run) sessions to the top of auto-follow purely
                # because the operator's GM panel polled them (story 126-34).
                session_id = _pg_sessions.resolve_session_id(pool, slug=slug)
                if session_id is None:
                    continue
                repository = PgSaveRepository(pool, session_id=session_id)
                saved = repository.load()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "debug_state.session_load_failed slug=%s error=%s",
                    slug,
                    exc,
                )
                continue
            if saved is None:
                continue
            # Project the loaded snapshot onto the SessionStateView wire shape.
            # The projection (snapshot → view dict) lives in a pure, unit-tested
            # helper so the GM-panel data contract can be exercised against
            # synthetic snapshots without the DB round-trip above. Story 124-4
            # corrected four silent dead reads that lived in the former inline
            # block (trope id/progression, NPC HP, player inventory).
            views.append(
                project_session_state_view(
                    saved.snapshot,
                    session_key=slug,
                    last_activity_ts=int(save_row.get("last_activity_ts") or 0),
                )
            )
        # Newest first — the dashboard's default "pick index 0" convention
        # then lands on the active session instead of the oldest save.
        views.sort(
            key=lambda v: int(v.get("last_activity_ts") or 0),
            reverse=True,
        )
        return views

    @router.get("/api/debug/saves")
    async def debug_saves(request: Request) -> list[dict[str, Any]]:
        """List saves for the forensics page. Read-only, lossy.

        ADR-115 D7: reads from ``PgForensicReader.list_saves()`` (all sessions
        with telemetry counts) instead of the SQLite save-file walk.
        """
        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg.forensic import PgForensicReader

        return PgForensicReader(_db_pool.get_pool()).list_saves()

    @router.get("/api/debug/save/{slug}/timeline")
    async def debug_save_timeline(request: Request, slug: str) -> list[dict[str, Any]]:
        """Round-keyed timeline for one save. [] if absent — never 500.

        ADR-115 D7: resolves slug→session_id then reads from
        ``PgForensicReader.build_timeline``. Unknown slug (resolve returns
        None) → [] (lossy-empty, the intended best-effort contract — NOT a
        silent SQLite fallback).
        """
        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions
        from sidequest.game.pg.forensic import PgForensicReader

        pool = _db_pool.get_pool()
        session_id = _pg_sessions.resolve_session_id(pool, slug=slug)
        if session_id is None:
            return []
        return PgForensicReader(pool).build_timeline(session_id)

    @router.get("/api/debug/save/{slug}/turn/{round_number}")
    async def debug_save_turn(request: Request, slug: str, round_number: int) -> dict[str, Any]:
        """Drill-down bundle for one round. Empty bundle if absent.

        ADR-115 D7: resolves slug→session_id then reads from
        ``PgForensicReader.build_turn_bundle``. Unknown slug → empty-but-shaped
        bundle (lossy-empty), never 500.
        """
        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions
        from sidequest.game.pg.forensic import PgForensicReader

        pool = _db_pool.get_pool()
        session_id = _pg_sessions.resolve_session_id(pool, slug=slug)
        if session_id is None:
            return {
                "round": round_number,
                "narrative": [],
                "events": [],
                "derived": {},
                "projection": [],
                "scrapbook": [],
                "unparseable_seqs": [],
                "telemetry": {"rows": [], "by_component": {}, "total": 0, "unparseable_seqs": []},
                "mechanical": {"state": "absent", "pcs": [], "trope": None, "unparseable_seqs": []},
            }
        return PgForensicReader(pool).build_turn_bundle(session_id, round_number)

    @router.get("/api/debug/save/{slug}/snapshot")
    async def debug_save_snapshot(request: Request, slug: str) -> dict[str, Any]:
        """Read-only persisted snapshot for the forensics 'final stored
        snapshot' panel. {} if absent — never 500, never writes.

        ADR-115 D7: resolves slug→session_id then reads the raw
        ``game_state.snapshot_json`` via ``PgForensicReader.snapshot_json``.
        Unknown slug → {} (lossy-empty).
        """
        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions
        from sidequest.game.pg.forensic import PgForensicReader

        pool = _db_pool.get_pool()
        session_id = _pg_sessions.resolve_session_id(pool, slug=slug)
        if session_id is None:
            return {}
        return PgForensicReader(pool).snapshot_json(session_id)

    @router.post("/api/games", status_code=201)
    async def create_game(req: CreateGameRequest, request: Request) -> Any:
        """Create a new game — always a fresh, unique session (201).

        The slug is ``<date>-<world>[-mp]-<token>`` with a per-game random
        token (``game_slug.generate_slug``), so every POST mints a distinct
        session. Resuming or joining an existing game is NOT done here — it
        happens by opening that game's exact ``/play/<slug>`` link (the lobby's
        Past Journeys history, or a shared link for co-play), which connects
        the WebSocket to the stored session directly.

        History (sq-playtest 2026-06-13): the slug used to be deterministic
        (``<date>-<world>[-mp]``), so a same-day same-world POST resumed the
        prior session and silently inherited its durable seat roster —
        deadlocking the MP turn barrier on a phantom, never-reconnecting seat
        (the Kael deadlock). Unique slugs remove that class at the root. The
        ``force_new`` field is accepted for request-shape back-compat but no
        longer consulted: every create is already fresh, and the deterministic
        ``-2``/``-3`` disambiguation + MP-join-by-rederivation short-circuit it
        drove are gone.
        """
        from sidequest.telemetry.spans import mp_game_created_span

        today_fn = getattr(request.app.state, "today_fn", _date_cls.today)
        today = today_fn()

        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions

        _pg_pool = _db_pool.get_pool()

        # Mint a unique slug. Regenerate on the astronomically-unlikely token
        # collision rather than attach to an existing row (No Silent Fallbacks:
        # a collision must never quietly resume a stranger's session).
        slug = generate_slug(world_slug=req.world_slug, today=today, mode=req.mode)
        attempts = 1
        while _pg_sessions.get_game(_pg_pool, slug=slug) is not None:
            attempts += 1
            if attempts > 8:
                raise HTTPException(
                    status_code=500,
                    detail="could not mint a unique game slug after 8 attempts",
                )
            slug = generate_slug(world_slug=req.world_slug, today=today, mode=req.mode)

        mode_str = str(req.mode.value) if hasattr(req.mode, "value") else str(req.mode)
        with mp_game_created_span(
            slug=slug,
            mode=mode_str,
            genre_slug=req.genre_slug,
            world_slug=req.world_slug,
            resumed=False,
            player_name=req.player_name or "",
            force_new=req.force_new,
            attempts=attempts,
        ):
            _pg_sessions.ensure_session(
                _pg_pool,
                slug=slug,
                mode=mode_str,
                genre_slug=req.genre_slug,
                world_slug=req.world_slug,
            )
            return GameResponse(
                slug=slug,
                mode=req.mode,
                genre_slug=req.genre_slug,
                world_slug=req.world_slug,
                resumed=False,
                player_name=req.player_name,
                orbital=_world_has_orbits(request, req.genre_slug, req.world_slug),
            )

    @router.get("/api/sessions/{slug}/encounter_events")
    async def get_encounter_events(slug: str, request: Request):
        """Return ordered ENCOUNTER_* event rows for the given session.

        ADR-115 D7: reads from the Postgres events table via
        ``PgForensicReader.encounter_events`` (was the SQLite events table).
        Used by the GM panel timeline view (Task 22). Unknown slug → 404
        (this endpoint's documented missing-session contract, distinct from
        the lossy-empty forensic reads).
        """
        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions
        from sidequest.game.pg.forensic import PgForensicReader

        pool = _db_pool.get_pool()
        session_id = _pg_sessions.resolve_session_id(pool, slug=slug)
        if session_id is None:
            raise HTTPException(status_code=404, detail=f"no game with slug {slug}")
        return PgForensicReader(pool).encounter_events(session_id)

    @router.get("/api/sessions/{slug}/assets")
    async def get_session_assets(slug: str, request: Request):
        """Return the runtime asset ledger for a save (Story 65-2).

        The UI fetches this on reconnect to rehydrate prior-turn imagery from
        R2 without re-rendering. Each row carries a resolved absolute ``url``
        (via the asset_urls seam) so the browser can load it directly — the
        raw ``r2_key`` alone is not a fetchable source. Unknown slug → 404
        (loud), never a silent empty list — a known session with no assets
        returns ``[]``.
        """
        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions
        from sidequest.game.pg.asset_ledger import PgAssetLedgerStore

        pool = _db_pool.get_pool()
        session_id = _pg_sessions.resolve_session_id(pool, slug=slug)
        if session_id is None:
            raise HTTPException(status_code=404, detail=f"no game with slug {slug}")
        rows = PgAssetLedgerStore(pool, session_id=session_id).list_assets()
        for row in rows:
            row["url"] = resolve_asset_url(str(row["r2_key"]))
        return rows

    @router.get("/api/games/{slug}")
    async def get_game_endpoint(slug: str, request: Request) -> GameResponse:
        """Return metadata for a game by slug.

        Raises 404 if no game with that slug exists.
        """
        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions

        row = _pg_sessions.get_game(_db_pool.get_pool(), slug=slug)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no game with slug {slug}")
        return GameResponse(
            slug=row.slug,
            mode=row.mode,
            genre_slug=row.genre_slug,
            world_slug=row.world_slug,
            resumed=True,
            orbital=_world_has_orbits(request, row.genre_slug, row.world_slug),
        )

    @router.get("/api/games/{slug}/hub")
    async def get_hub_state(slug: str, request: Request) -> dict:
        """Return WorldSave + enriched dungeon list for a hub-world game.

        404: slug not found. 409: world has no dungeons (not_a_hub_world).
        200: WorldSave JSON + available_dungeons [{slug, sin, wounded}, ...].
        """
        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions
        from sidequest.game.pg.save_repository import PgSaveRepository

        _pg_pool = _db_pool.get_pool()
        row = _pg_sessions.get_game(_pg_pool, slug=slug)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no game with slug {slug}")

        search_paths = getattr(
            request.app.state,
            "genre_pack_search_paths",
            DEFAULT_GENRE_PACK_SEARCH_PATHS,
        )
        genre_pack = load_genre_pack_cached(row.genre_slug, search_paths=search_paths)
        world = genre_pack.worlds.get(row.world_slug)
        # Post–Sünden world shape (revert 51822a2 + re-fold 2026-05-10): dungeons
        # are no longer a separate ``World.dungeons`` map — they are regions in
        # ``world.cartography.regions`` tagged with ``terrain: dungeon``. The
        # ``sin`` attribute rides as a Region extra (``Region.model_config = {"extra": "allow"}``).
        dungeon_regions: dict[str, Any] = {}
        if world is not None:
            for region_slug, region in world.cartography.regions.items():
                if (region.terrain or "").lower() == "dungeon":
                    dungeon_regions[region_slug] = region
        if world is None or not dungeon_regions:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "not_a_hub_world",
                    "world_slug": row.world_slug,
                    "reason": "world has no dungeon-terrain regions",
                },
            )

        # ADR-115 D2: load WorldSave from PG repository.
        repository = PgSaveRepository.for_slug(
            _pg_pool,
            slug=slug,
            mode=row.mode,
            genre_slug=row.genre_slug,
            world_slug=row.world_slug,
        )
        world_save = repository.load_world_save()
        available_dungeons = [
            {
                "slug": dungeon_slug,
                "sin": getattr(region, "sin", "") or "",
                "wounded": world_save.dungeon_wounds.get(dungeon_slug, False),
            }
            for dungeon_slug, region in sorted(dungeon_regions.items())
        ]
        return {
            "slug": slug,
            "genre_slug": row.genre_slug,
            "world_slug": row.world_slug,
            "available_dungeons": available_dungeons,
            "world_save": world_save.model_dump(mode="json"),
        }

    # -----------------------------------------------------------------------
    # Story 91-5 — Dark-spend reconciliation endpoints
    # -----------------------------------------------------------------------

    @router.get("/api/debug/cost/instrumented")
    async def get_instrumented_cost() -> dict[str, Any]:
        """Return the process-level instrumented Anthropic spend from the
        SessionCostLedger. Used by the dark-spend reconciliation script and
        the GM dashboard Layer 1 panel."""
        from sidequest.agents.cost_safety import ledger as _ledger

        cost_ledger = _ledger()
        return {
            "instrumented_usd": cost_ledger.instrumented_total_usd(),
            "session_count": len(cost_ledger.cumulative_cost_usd),
        }

    @router.get("/api/debug/cost/reconciliation")
    async def get_reconciliation() -> dict[str, Any] | None:
        """Return the latest dark-spend reconciliation result POSTed by the
        reconciliation script, or null if no reconciliation has run yet."""
        return _reconciliation_store.get("latest")

    @router.post("/api/debug/cost/reconciliation", status_code=200)
    async def post_reconciliation(result: _ReconcileResultPayload) -> dict[str, Any]:
        """Store a reconciliation result from the reconciliation script and
        fire the dark_spend.gap_detected watcher event if alert=True."""
        payload = result.model_dump()
        _reconciliation_store["latest"] = payload

        if result.alert:
            from sidequest.telemetry.watcher_hub import publish_event as _pub

            _pub(
                "dark_spend.gap_detected",
                {
                    "gap_pct": result.gap_pct,
                    "billed_usd": result.billed_usd,
                    "instrumented_usd": result.instrumented_usd,
                    "alert": True,
                },
                component="cost_reconcile",
                severity="error",
            )
        return payload

    @router.get("/api/chargen/portraits/{genre}/{world}")
    async def list_chargen_portraits(genre: str, world: str, request: Request) -> dict[str, Any]:
        """List player-picker sample portraits for a world (Epic 66).

        Filters portrait_manifest entries to type=player_picker. Returns an
        empty list (not an error) for worlds that ship no pickers, or for an
        unknown world within a valid genre.
        """
        search_paths: list[Path] = getattr(
            request.app.state,
            "genre_pack_search_paths",
            DEFAULT_GENRE_PACK_SEARCH_PATHS,
        )
        genre_pack = load_genre_pack_cached(genre, search_paths=search_paths)
        world_obj = genre_pack.worlds.get(world)
        portraits: list[dict[str, Any]] = []
        if world_obj is not None:
            for entry in world_obj.portrait_manifest:
                if entry.character_type != "player_picker":
                    continue
                slug = picker_portrait_slug(entry)
                portraits.append(
                    {
                        "slug": slug,
                        "culture": entry.culture,
                        "archetype": entry.archetype,
                        "sex": entry.sex,
                        "role": entry.role,
                        "portrait_url": resolve_player_portrait_url(genre, world, slug),
                    }
                )
        return {"portraits": portraits}

    @router.get("/api/chassis/{instance_id}/interior")
    def get_chassis_interior(instance_id: str, request: Request):
        search_paths: list[Path] = getattr(
            request.app.state,
            "genre_pack_search_paths",
            DEFAULT_GENRE_PACK_SEARCH_PATHS,
        )
        chassis_class, chassis_inst, _genre_slug, _world_slug = _find_chassis_instance(
            search_paths, instance_id
        )
        if chassis_class is None or chassis_inst is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"chassis instance {instance_id!r} not found in any genre pack on the search path"
                ),
            )

        # The runtime ChassisInstance carries the same id/name/class_id the
        # renderer reads; we can pass the YAML config directly because the
        # renderer only touches those three attributes.
        class _InstView:
            pass

        inst_view = _InstView()
        inst_view.id = chassis_inst.id
        inst_view.name = chassis_inst.name
        inst_view.class_id = chassis_inst.chassis_class_id

        # The default crew NPCs (kestrel_captain etc.) are hardcoded in
        # the renderer's KESTREL_NPC_DEFAULT_ROOM table. To make them
        # visible against an empty snapshot, synthesize lightweight NPC
        # actor stubs for each crew_npcs entry. When this endpoint is
        # later wired to the live session, the real snapshot.npcs takes
        # over and this synthesis is bypassed.
        class _StubActor:
            def __init__(self, name: str):
                self.core = type("Core", (), {"name": name})()
                self.current_room = None

        snapshot = _EmptySnapshot()
        snapshot.npcs = [_StubActor(npc_id) for npc_id in chassis_inst.crew_npcs]

        svg = render_interior_svg(chassis_class, inst_view, snapshot)

        emit_interior_render(
            chassis_instance_id=instance_id,
            actor_count=len(snapshot.npcs),
            tracked_pcs=0,
            tracked_npcs=len(snapshot.npcs),
            output_size_bytes=len(svg.encode("utf-8")),
        )
        return Response(content=svg, media_type="image/svg+xml")

    from sidequest.server.bug_report import register_bug_report_routes

    register_bug_report_routes(router)

    return router


# ---------------------------------------------------------------------------
# In-process reconciliation store (process-lifetime, no persistence needed —
# the script POSTs the result on every run; a server restart is a fresh day).
# ---------------------------------------------------------------------------
_reconciliation_store: dict[str, Any] = {}


class _ReconcileResultPayload(BaseModel):
    """Pydantic model for the POST /api/debug/cost/reconciliation body.

    All fields are required — a partial payload makes gap_pct uncomputable.
    """

    instrumented_usd: float
    billed_usd: float
    gap_pct: float
    alert: bool
