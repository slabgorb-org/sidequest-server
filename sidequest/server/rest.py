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

from sidequest.game.forensic_query import (
    _safe_json,
    build_timeline,
    build_turn_bundle,
    list_saves,
    open_save_readonly,
)
from sidequest.game.game_slug import generate_slug
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    get_game,
    upsert_game,
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
                            craw = yaml.safe_load(
                                cart_yaml_path.read_text(encoding="utf-8")
                            )
                            navigation_mode = str(
                                (craw or {}).get("navigation_mode", "region")
                            )
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
        """
        save_dir: Path = request.app.state.save_dir
        games_root = save_dir / "games"
        views: list[dict[str, Any]] = []
        if not games_root.exists():
            return views
        # Enumerate candidate slug dirs, optionally filtered to a single
        # session_key so the dashboard can target the active session
        # directly (playtest 2026-04-24 — the State tab defaulted to
        # index 0 which was the oldest save, not the active one).
        if session_key is not None:
            candidates = [games_root / session_key]
        else:
            candidates = sorted(games_root.iterdir())
        for slug_dir in candidates:
            if not slug_dir.is_dir():
                continue
            db_file = slug_dir / "save.db"
            if not db_file.is_file():
                continue
            try:
                store = SqliteStore.open(str(db_file))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "debug_state.store_open_failed slug=%s error=%s",
                    slug_dir.name,
                    exc,
                )
                continue
            try:
                saved = store.load()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "debug_state.snapshot_load_failed slug=%s error=%s",
                    slug_dir.name,
                    exc,
                )
                store.close()
                continue
            store.close()
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
                hp = getattr(char, "hp", None)
                max_hp = getattr(char, "hp_max", None)
                # Character.name / Character.level are Combatant-equivalent
                # methods (Rust port — see sidequest/game/character.py:148-162),
                # not attributes. getattr returns the bound method; call it.
                name_attr = getattr(char, "name", None)
                level_attr = getattr(char, "level", 1)
                resolved_name = name_attr() if callable(name_attr) else name_attr
                resolved_level = level_attr() if callable(level_attr) else level_attr
                players.append(
                    {
                        "player_name": getattr(char, "player_name", "") or "",
                        "character_name": resolved_name,
                        "character_class": getattr(char, "archetype", "") or "",
                        "character_hp": int(hp) if hp is not None else 0,
                        "character_max_hp": int(max_hp) if max_hp is not None else 0,
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
            try:
                last_activity_ts = int(db_file.stat().st_mtime * 1000)
            except OSError:
                last_activity_ts = 0
            views.append(
                {
                    "session_key": slug_dir.name,
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
        """List local saves for the forensics page. Read-only, lossy."""
        return list_saves(request.app.state.save_dir)

    @router.get("/api/debug/save/{slug}/timeline")
    async def debug_save_timeline(request: Request, slug: str) -> list[dict[str, Any]]:
        """Round-keyed timeline for one save. [] if absent/broken — never 500."""
        conn = open_save_readonly(request.app.state.save_dir, slug)
        if conn is None:
            return []
        try:
            return build_timeline(conn)
        except Exception:  # noqa: BLE001 — never 500 a forensics read
            logger.warning("forensic.timeline_failed slug=%s", slug, exc_info=True)
            return []
        finally:
            conn.close()

    @router.get("/api/debug/save/{slug}/turn/{round_number}")
    async def debug_save_turn(request: Request, slug: str, round_number: int) -> dict[str, Any]:
        """Drill-down bundle for one round. Empty bundle if absent/broken."""
        empty = {
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
        conn = open_save_readonly(request.app.state.save_dir, slug)
        if conn is None:
            return empty
        try:
            return build_turn_bundle(conn, round_number)
        except Exception:  # noqa: BLE001 — never 500 a forensics read
            logger.warning(
                "forensic.turn_failed slug=%s round=%s",
                slug,
                round_number,
                exc_info=True,
            )
            return empty
        finally:
            conn.close()

    @router.get("/api/debug/save/{slug}/snapshot")
    async def debug_save_snapshot(request: Request, slug: str) -> dict[str, Any]:
        """Read-only persisted snapshot for the forensics 'final stored
        snapshot' panel. {} if absent/broken — never 500, never writes."""
        conn = open_save_readonly(request.app.state.save_dir, slug)
        if conn is None:
            return {}
        try:
            row = conn.execute("SELECT snapshot_json FROM game_state WHERE id = 1").fetchone()
            if row is None or row[0] is None:
                return {}
            parsed = _safe_json(row[0])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001 — never 500 a forensics read
            logger.warning("forensic.snapshot_failed slug=%s", slug, exc_info=True)
            return {}
        finally:
            conn.close()

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

        save_dir: Path = request.app.state.save_dir
        today_fn = getattr(request.app.state, "today_fn", _date_cls.today)
        base_slug = generate_slug(world_slug=req.world_slug, today=today_fn(), mode=req.mode)

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
            probe_db = db_path_for_slug(save_dir, slug)
            if probe_db.exists():
                probe_store = SqliteStore(probe_db)
                probe_store.initialize()
                existing_row = get_game(probe_store, slug)
                if existing_row is not None:
                    is_mp_request = req.mode == GameMode.MULTIPLAYER
                    is_mp_existing = existing_row.mode == GameMode.MULTIPLAYER
                    if is_mp_request and is_mp_existing:
                        # MP-join short-circuit. Fall through to the
                        # existing-row branch below; the join span fires
                        # there once the row is opened on the canonical
                        # ``store`` handle (avoids span-on-probe drift).
                        mp_join_existing = True
                    else:
                        while True:
                            attempts += 1
                            candidate = f"{base_slug}-{attempts}"
                            cand_db = db_path_for_slug(save_dir, candidate)
                            if not cand_db.exists():
                                slug = candidate
                                break
                            cand_store = SqliteStore(cand_db)
                            cand_store.initialize()
                            if get_game(cand_store, candidate) is None:
                                slug = candidate
                                break
                        with lobby_force_new_disambiguated_span(
                            requested_slug=base_slug,
                            final_slug=slug,
                            attempts=attempts,
                            player_name=req.player_name or "",
                            mode=str(req.mode.value)
                            if hasattr(req.mode, "value")
                            else str(req.mode),
                            genre_slug=req.genre_slug,
                            world_slug=req.world_slug,
                        ):
                            pass

        db = db_path_for_slug(save_dir, slug)
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        store.initialize()

        existing = get_game(store, slug)
        if existing is not None:
            # Existing row "wins" — emit span with the frozen metadata so GM
            # panel sees which mode/genre/world are actually in effect, not
            # what the client requested. (force_new path can land here ONLY
            # via the MP-join short-circuit above; solo force_new+collision
            # always picked an unused slug.)
            if mp_join_existing:
                with lobby_session_join_existing_span(
                    slug=slug,
                    mode=str(existing.mode.value)
                    if hasattr(existing.mode, "value")
                    else str(existing.mode),
                    genre_slug=existing.genre_slug,
                    world_slug=existing.world_slug,
                    player_name=req.player_name or "",
                    force_new_requested=True,
                ):
                    pass
            with mp_game_created_span(
                slug=slug,
                mode=str(existing.mode.value)
                if hasattr(existing.mode, "value")
                else str(existing.mode),
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
            upsert_game(
                store,
                slug=slug,
                mode=req.mode,
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
            )

    @router.get("/api/sessions/{slug}/encounter_events")
    async def get_encounter_events(slug: str, request: Request):
        """Return ordered ENCOUNTER_* event rows for the given session.

        Reads from the SQLite events table populated by the watcher hub
        (Task 20). Used by the GM panel timeline view (Task 22).
        """
        save_dir: Path = request.app.state.save_dir
        db = db_path_for_slug(save_dir, slug)
        if not db.exists():
            raise HTTPException(status_code=404, detail=f"no game with slug {slug}")
        store = SqliteStore(db)
        store.initialize()
        from sidequest.game.persistence import query_encounter_events

        return query_encounter_events(store)

    @router.get("/api/games/{slug}")
    async def get_game_endpoint(slug: str, request: Request) -> GameResponse:
        """Return metadata for a game by slug.

        Raises 404 if no game with that slug exists.
        """
        save_dir: Path = request.app.state.save_dir
        db = db_path_for_slug(save_dir, slug)
        if not db.exists():
            raise HTTPException(status_code=404, detail=f"no game with slug {slug}")
        store = SqliteStore(db)
        store.initialize()
        row = get_game(store, slug)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no game with slug {slug}")
        return GameResponse(
            slug=row.slug,
            mode=row.mode,
            genre_slug=row.genre_slug,
            world_slug=row.world_slug,
            resumed=True,
        )

    @router.get("/api/games/{slug}/hub")
    async def get_hub_state(slug: str, request: Request) -> dict:
        """Return WorldSave + enriched dungeon list for a hub-world game.

        404: slug not found. 409: world has no dungeons (not_a_hub_world).
        200: WorldSave JSON + available_dungeons [{slug, sin, wounded}, ...].
        """
        save_dir: Path = request.app.state.save_dir
        db = db_path_for_slug(save_dir, slug)
        if not db.exists():
            raise HTTPException(status_code=404, detail=f"no game with slug {slug}")
        store = SqliteStore(db)
        store.initialize()
        row = get_game(store, slug)
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

        world_save = store.load_world_save()
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

    return router
