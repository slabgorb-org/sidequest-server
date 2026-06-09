"""FastAPI routes for the player-facing reference JSON projection API.

Routes:
    GET /reference/api/lore/{pack}/{world} — world-tier public-projected JSON
    GET /reference/api/rules/{pack}        — pack-tier public-projected JSON

Story 100-12 (Phase 4 cutover) retired the server-rendered HTML routes
(``/reference/rules/{pack}`` and ``/reference/lore/{pack}/{world}``) and the
``/reference/static/*`` asset route. Those URLs now fall through to the React
SPA shell via the history-fallback catch-all (``_install_spa_fallback`` in
``app.py``), and the SPA fetches these JSON projections over REST. This module
owns the surviving HTTP boundary: registry lookups, 404/500, response shaping.
The projection is pure (``sidequest.server.reference_projection``); the
``reference_visibility.py`` firewall is the load-bearing keeper-field gate.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from sidequest.server.reference_projection import (
    build_lore_projection,
    build_rules_projection,
    build_theme_tokens,
)
from sidequest.server.reference_theme import MissingThemeFieldError

_LOG = logging.getLogger(__name__)
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _resolve_pack_dir(request: Request, pack: str) -> Path:
    """Find the on-disk dir for ``pack``, or raise 404 with valid alternatives."""
    if not _SAFE_SLUG.match(pack):
        raise HTTPException(status_code=404, detail=f"Unknown pack: {pack}")
    paths = getattr(request.app.state, "genre_pack_search_paths", None) or []
    if not paths:
        raise HTTPException(
            status_code=500,
            detail="No genre pack search paths configured on app.state.",
        )
    for root in paths:
        candidate = Path(root) / pack
        if candidate.is_dir():
            return candidate
    valid = sorted(
        {
            entry.name
            for root in paths
            if Path(root).is_dir()
            for entry in Path(root).iterdir()
            if entry.is_dir() and _SAFE_SLUG.match(entry.name)
        }
    )
    raise HTTPException(
        status_code=404,
        detail=f"Pack '{pack}' not found. Valid packs: {', '.join(valid) or '(none)'}",
    )


def _resolve_world_dir(pack_dir: Path, world: str) -> Path:
    if not _SAFE_SLUG.match(world):
        raise HTTPException(status_code=404, detail=f"Unknown world: {world}")
    candidate = pack_dir / "worlds" / world
    if candidate.is_dir():
        return candidate
    worlds_root = pack_dir / "worlds"
    valid = (
        sorted(
            entry.name
            for entry in worlds_root.iterdir()
            if entry.is_dir() and _SAFE_SLUG.match(entry.name)
        )
        if worlds_root.is_dir()
        else []
    )
    raise HTTPException(
        status_code=404,
        detail=(
            f"World '{world}' not found in pack '{pack_dir.name}'. "
            f"Valid worlds: {', '.join(valid) or '(none)'}"
        ),
    )


def create_reference_router() -> APIRouter:
    router = APIRouter(prefix="/reference", tags=["reference"])

    @router.get("/api/lore/{pack}/{world}", response_class=JSONResponse)
    async def lore_api(request: Request, pack: str, world: str) -> JSONResponse:
        pack_dir = _resolve_pack_dir(request, pack)
        world_dir = _resolve_world_dir(pack_dir, world)
        try:
            doc = build_lore_projection(pack, world, pack_dir=pack_dir, world_dir=world_dir)
            # Story 100-7: the session-free theme token set rides on the same
            # projection doc the React injector consumes (top-level "theme").
            doc["theme"] = build_theme_tokens(pack, pack_dir=pack_dir)
        except (ValueError, FileNotFoundError, MissingThemeFieldError) as exc:
            _LOG.exception("reference lore api: projection failed for %s/%s", pack, world)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(content=doc)

    @router.get("/api/rules/{pack}", response_class=JSONResponse)
    async def rules_api(request: Request, pack: str) -> JSONResponse:
        pack_dir = _resolve_pack_dir(request, pack)
        try:
            doc = build_rules_projection(pack, pack_dir=pack_dir)
            # Story 100-7: attach the CSS-var theme token set at the route layer
            # (not inside build_rules_projection — that would break the empty-pack
            # omits-absent-files contract). Same top-level "theme" key as lore.
            doc["theme"] = build_theme_tokens(pack, pack_dir=pack_dir)
        except (ValueError, MissingThemeFieldError) as exc:
            _LOG.exception("reference rules api: projection failed for %s", pack)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(content=doc)

    return router
