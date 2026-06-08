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
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sidequest.game.game_slug import generate_slug
from sidequest.game.persistence import (
    GameMode,
)
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, load_genre_pack_cached
from sidequest.server.asset_urls import resolve_asset_url

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
    # Silent-resume masquerade (sq-playtest 2026-06-07): the names of the
    # characters already in the session when ``resumed=True`` — the lobby
    # announces "resuming existing table — Groucho, Chico" instead of
    # presenting a resume as a fresh creation. Empty for fresh games and
    # for resumed sessions with no persisted snapshot yet.
    existing_characters: list[str] = []


def _existing_character_names(pool: Any, slug: str) -> list[str]:
    """Character names from the persisted snapshot of ``slug``'s session.

    Read-only, lossy-empty: no session / no snapshot / unparseable snapshot
    all yield ``[]`` (the forensics reader's documented contract) — the
    create endpoint must never 500 on a forensic read.
    """
    from sidequest.game.pg import sessions as _pg_sessions
    from sidequest.game.pg.forensic import PgForensicReader

    session_id = _pg_sessions.resolve_session_id(pool, slug=slug)
    if session_id is None:
        return []
    snapshot = PgForensicReader(pool).snapshot_json(session_id)
    names: list[str] = []
    for character in (snapshot or {}).get("characters") or []:
        if not isinstance(character, dict):
            continue
        core = character.get("core") or {}
        name = str(core.get("name") or "").strip() if isinstance(core, dict) else ""
        if name:
            names.append(name)
    return names


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

        Walks ``<save_dir>/games/<slug>/save.db`` and projects each loaded
        :class:`GameSnapshot` onto the ``SessionStateView`` shape defined in
        ``sidequest-ui/src/types/watcher.ts``. Read-only; broken / empty DB
        files are skipped rather than failing the request.

        Results are sorted by save-file modification time, newest first —
        so the dashboard's default "index 0" pick lands on the
        most-recently-touched session rather than an old save. Each view
        includes ``last_activity_ts`` (ms since epoch) so the UI can also
        pick explicitly.

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
            # session row with a mode value GameMode() rejects, or a snapshot
            # that load() can't deserialize — must not 500 the entire State
            # tab. Log loudly (No-Silent-Fallbacks: this is observable, not a
            # quiet alternative path) and skip just that slug.
            try:
                game = _pg_sessions.get_game(pool, slug=slug)
                if game is None:
                    continue
                repository = PgSaveRepository.for_slug(
                    pool,
                    slug=slug,
                    mode=GameMode(game.mode),
                    genre_slug=game.genre_slug,
                    world_slug=game.world_slug,
                )
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
            snap = saved.snapshot
            # Wave 2A snapshot split: NPCs live in two canonical stores —
            # ``snap.npcs`` (mechanically engaged, carries CreatureCore +
            # last_seen tracking) and ``snap.npc_pool`` (identity-only
            # cast pool members). The legacy ``snap.npc_registry`` field
            # was dropped in story 45-52; legacy saves migrate into
            # ``snap.npc_pool`` on load. Project both canonical stores so
            # the GM panel actually surfaces the NPCs that exist (the
            # panel's JSON field name stays ``npc_registry`` to preserve
            # the wire contract).
            npc_registry: list[dict[str, Any]] = []
            for npc in snap.npcs:
                core = npc.core
                edge = getattr(core, "edge", None)
                hp_current = (
                    int(edge.current) if edge is not None and edge.current is not None else 0
                )
                hp_max = int(edge.maximum) if edge is not None and edge.maximum is not None else 0
                npc_registry.append(
                    {
                        "name": core.name or "",
                        "pronouns": npc.pronouns or "",
                        "role": npc.npc_role_id or "",
                        "location": npc.last_seen_location or npc.location or "",
                        "last_seen_turn": npc.last_seen_turn or 0,
                        "age": npc.age or "",
                        "appearance": npc.appearance or "",
                        "ocean_summary": None,
                        "ocean": npc.ocean,
                        "hp": hp_current,
                        "max_hp": hp_max,
                    }
                )
            for member in snap.npc_pool:
                # Pool members are identity-only — no HP or last_seen.
                npc_registry.append(
                    {
                        "name": member.name or "",
                        "pronouns": member.pronouns or "",
                        "role": member.role or "",
                        "location": "",
                        "last_seen_turn": 0,
                        "age": "",
                        "appearance": member.appearance or "",
                        "ocean_summary": None,
                        "ocean": None,
                        "hp": 0,
                        "max_hp": 0,
                    }
                )
            trope_states: list[dict[str, Any]] = []
            for trope in snap.active_tropes:
                trope_states.append(
                    {
                        "trope_definition_id": getattr(trope, "trope_id", ""),
                        "status": str(getattr(trope, "status", "")),
                        "progression": int(getattr(trope, "progression", 0) or 0),
                    }
                )
            players: list[dict[str, Any]] = []
            for char in snap.characters:
                # Character.name / Character.level / Character.hp / Character.max_hp
                # are Combatant-equivalent methods (Rust port — see
                # sidequest/game/character.py:148-162), not attributes.
                # getattr returns the bound method; call it.
                name_attr = getattr(char, "name", None)
                level_attr = getattr(char, "level", 1)
                hp_attr = getattr(char, "hp", None)
                max_hp_attr = getattr(char, "max_hp", None)
                resolved_name = name_attr() if callable(name_attr) else name_attr
                resolved_level = level_attr() if callable(level_attr) else level_attr
                resolved_hp = hp_attr() if callable(hp_attr) else hp_attr
                resolved_max_hp = max_hp_attr() if callable(max_hp_attr) else max_hp_attr
                players.append(
                    {
                        "player_name": getattr(char, "player_name", "") or "",
                        "character_name": resolved_name,
                        "character_class": getattr(char, "archetype", "") or "",
                        "character_hp": int(resolved_hp) if resolved_hp is not None else 0,
                        "character_max_hp": int(resolved_max_hp)
                        if resolved_max_hp is not None
                        else 0,
                        "character_level": int(resolved_level or 1),
                        "character_xp": int(getattr(char, "xp", 0) or 0),
                        "region_id": snap.current_region or "",
                        "display_location": (snap.character_locations.get(resolved_name) or ""),
                        "inventory": {
                            "items": [],
                            "gold": 0,
                        },
                    }
                )
            last_activity_ts = int(save_row.get("last_activity_ts") or 0)
            views.append(
                {
                    "session_key": slug,
                    "genre_slug": snap.genre_slug or "",
                    "world_slug": snap.world_slug or "",
                    "current_location": snap.party_location() or "",
                    "discovered_regions": list(snap.discovered_regions),
                    "narration_history_len": len(snap.narrative_log),
                    "turn_mode": str(snap.turn_manager.phase),
                    "npc_registry": npc_registry,
                    "trope_states": trope_states,
                    "players": players,
                    "player_count": len(players),
                    "has_music_director": False,
                    "has_audio_mixer": False,
                    "region_names": [],
                    "last_activity_ts": last_activity_ts,
                }
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
    async def create_or_resume_game(req: CreateGameRequest, request: Request) -> Any:
        """Create a new game (201) or resume an existing same-slug game (200, resumed=True).

        The slug is derived from world_slug + today's date. If a game already
        exists for that slug, it is returned in frozen mode — the original mode,
        genre_slug, and world_slug are preserved and the new request's mode is
        ignored.

        Lobby contract (companions to sidequest-ui develop 1436ebd):
          - ``player_name``: typed name from the lobby; threaded onto the
            response so the UI can confirm the server received it.
          - ``force_new``: when True, a colliding base slug is *not* returned
            as a resume — instead the server appends a numeric disambiguator
            (``-2``, ``-3``, ...) and emits ``lobby.force_new_disambiguated``.
        """
        from sidequest.telemetry.spans import (
            lobby_force_new_disambiguated_span,
            lobby_session_join_existing_span,
            mp_game_created_span,
        )

        today_fn = getattr(request.app.state, "today_fn", _date_cls.today)
        base_slug = generate_slug(world_slug=req.world_slug, today=today_fn(), mode=req.mode)

        from sidequest.game import db_pool as _db_pool
        from sidequest.game.pg import sessions as _pg_sessions

        _pg_pool = _db_pool.get_pool()

        # ----- force_new: disambiguate before touching the store ---------
        # When the lobby insists this is a fresh journey, a same-day same-mode
        # collision must not silently resume the prior session. Walk -2, -3,
        # ... until we find an unclaimed slug.
        #
        # MP-mode exception (playtest 2026-04-26 S4-UX): the lobby's
        # ``force_new`` heuristic compares the typed name against the
        # **per-browser** Past Journey list. Across hosts (P1 on
        # ``player1.local``, P2 on ``player2.local``) that list is empty
        # for P2, so the UI always sends ``force_new=True`` — and the
        # disambiguator faithfully splits the table by minting ``-2``.
        # In MP mode the correct semantics are "join the existing
        # same-day same-world MP session", so we ignore ``force_new``
        # whenever the existing same-slug game is itself a multiplayer
        # game. Solo journeys are per-player and keep the original
        # disambiguation behavior unchanged.
        slug = base_slug
        attempts = 1
        mp_join_existing = False
        if req.force_new:
            existing_row = _pg_sessions.get_game(_pg_pool, slug=slug)
            if existing_row is not None:
                is_mp_request = req.mode == GameMode.MULTIPLAYER
                is_mp_existing = existing_row.mode == GameMode.MULTIPLAYER
                if is_mp_request and is_mp_existing:
                    # MP-join short-circuit. Fall through to the
                    # existing-row branch below; the join span fires
                    # there once the row is opened on the canonical
                    # pg repository (avoids span-on-probe drift).
                    mp_join_existing = True
                else:
                    while True:
                        attempts += 1
                        candidate = f"{base_slug}-{attempts}"
                        if _pg_sessions.get_game(_pg_pool, slug=candidate) is None:
                            slug = candidate
                            break
                    with lobby_force_new_disambiguated_span(
                        requested_slug=base_slug,
                        final_slug=slug,
                        attempts=attempts,
                        player_name=req.player_name or "",
                        mode=str(req.mode.value) if hasattr(req.mode, "value") else str(req.mode),
                        genre_slug=req.genre_slug,
                        world_slug=req.world_slug,
                    ):
                        pass

        existing = _pg_sessions.get_game(_pg_pool, slug=slug)
        if existing is not None:
            # Existing row "wins" — emit span with the frozen metadata so GM
            # panel sees which mode/genre/world are actually in effect, not
            # what the client requested. (force_new path can land here ONLY
            # via the MP-join short-circuit above; solo force_new+collision
            # always picked an unused slug.)
            if mp_join_existing:
                with lobby_session_join_existing_span(
                    slug=slug,
                    mode=str(existing.mode)
                    if not hasattr(existing.mode, "value")
                    else str(existing.mode.value),
                    genre_slug=existing.genre_slug,
                    world_slug=existing.world_slug,
                    player_name=req.player_name or "",
                    force_new_requested=True,
                ):
                    pass
            with mp_game_created_span(
                slug=slug,
                mode=str(existing.mode)
                if not hasattr(existing.mode, "value")
                else str(existing.mode.value),
                genre_slug=existing.genre_slug,
                world_slug=existing.world_slug,
                resumed=True,
            ):
                payload = GameResponse(
                    slug=slug,
                    mode=existing.mode,
                    genre_slug=existing.genre_slug,
                    world_slug=existing.world_slug,
                    resumed=True,
                    player_name=req.player_name,
                    orbital=_world_has_orbits(request, existing.genre_slug, existing.world_slug),
                    # Silent-resume masquerade fix: name the existing table so
                    # the lobby can announce the resume.
                    existing_characters=_existing_character_names(_pg_pool, slug),
                )
                return JSONResponse(status_code=200, content=payload.model_dump())

        with mp_game_created_span(
            slug=slug,
            mode=str(req.mode.value) if hasattr(req.mode, "value") else str(req.mode),
            genre_slug=req.genre_slug,
            world_slug=req.world_slug,
            resumed=False,
            player_name=req.player_name or "",
            force_new=req.force_new,
        ):
            _pg_sessions.ensure_session(
                _pg_pool,
                slug=slug,
                mode=str(req.mode.value) if hasattr(req.mode, "value") else str(req.mode),
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

        l = _ledger()
        return {
            "instrumented_usd": l.instrumented_total_usd(),
            "session_count": len(l.cumulative_cost_usd),
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
                severity="warn",
            )
        return payload

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
