"""FastAPI routes for the player-facing reference pages.

Routes:
    GET /reference/rules/{pack}            — pack-tier YAML rendered as HTML
    GET /reference/lore/{pack}/{world}     — world-tier + pack flavor as HTML

The renderer is pure (sidequest.server.reference_renderer). This module owns
the HTTP boundary: registry lookups, 404/500, response shaping.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from sidequest.server.reference_projection import build_lore_projection
from sidequest.server.reference_renderer import (
    assemble_lore_page,
    assemble_rules_page,
)
from sidequest.server.reference_theme import MissingThemeFieldError

_LOG = logging.getLogger(__name__)
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Static-asset filename: lowercase alnum with one extension. No "..", no slashes.
_SAFE_STATIC_FILENAME = re.compile(r"^[a-z0-9][a-z0-9_-]*\.[a-z0-9]+$")


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

    # Use a dedicated subdirectory so the /reference/static route surface
    # never accidentally aliases other files in sidequest/server/static/
    # (dashboard.html, forensics.html, future package-level assets).
    static_dir = Path(__file__).parent / "static" / "reference"

    # NOTE: We expose /reference/static/* via explicit APIRoutes rather than
    # router.mount(StaticFiles(...)), because FastAPI's APIRouter.include_router
    # silently drops Mount routes from sub-routers (only APIRoute / Route /
    # WebSocketRoute propagate). The HTML's
    # <link href="/reference/static/theme.css"> still resolves correctly.
    @router.get("/static/{filename}", include_in_schema=False)
    async def static_file(filename: str) -> FileResponse:
        if not _SAFE_STATIC_FILENAME.match(filename):
            raise HTTPException(status_code=404, detail=f"Unknown static asset: {filename}")
        candidate = static_dir / filename
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail=f"Unknown static asset: {filename}")
        media_type = "text/css" if filename.endswith(".css") else None
        return FileResponse(str(candidate), media_type=media_type)

    @router.get("/rules/{pack}", response_class=HTMLResponse)
    async def rules_page(request: Request, pack: str) -> HTMLResponse:
        pack_dir = _resolve_pack_dir(request, pack)
        try:
            html = assemble_rules_page(pack, pack_dir)
        except (ValueError, MissingThemeFieldError) as exc:
            _LOG.exception("reference rules page: render failed for %s", pack)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return HTMLResponse(content=html)

    @router.get("/lore/{pack}/{world}", response_class=HTMLResponse)
    async def lore_page(request: Request, pack: str, world: str) -> HTMLResponse:
        pack_dir = _resolve_pack_dir(request, pack)
        world_dir = _resolve_world_dir(pack_dir, world)
        try:
            html = assemble_lore_page(pack, world, pack_dir, world_dir)
        except (ValueError, FileNotFoundError, MissingThemeFieldError) as exc:
            # FileNotFoundError: a feature-bearing world (POIs or Cast) whose
            # r2_manifest.json is absent — fail loud (500), never a silently
            # image-free page (Story 65-9 AC2, No Silent Fallbacks).
            _LOG.exception("reference lore page: render failed for %s/%s", pack, world)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return HTMLResponse(content=html)

    @router.get("/api/lore/{pack}/{world}")
    async def lore_api(request: Request, pack: str, world: str) -> JSONResponse:
        pack_dir = _resolve_pack_dir(request, pack)
        world_dir = _resolve_world_dir(pack_dir, world)
        try:
            doc = build_lore_projection(pack, world, pack_dir=pack_dir, world_dir=world_dir)
        except (ValueError, FileNotFoundError, MissingThemeFieldError) as exc:
            _LOG.exception("reference lore api: projection failed for %s/%s", pack, world)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(content=doc)

    return router
