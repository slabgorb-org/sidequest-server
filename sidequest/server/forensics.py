"""HTTP route that serves the Save Forensics page.

Read-only post-mortem counterpart to the live OTEL dashboard. A single
self-contained HTML file under ``sidequest/server/static/``; it fetches
the ``/api/debug/save*`` JSON endpoints on the same origin. No WebSocket.
Exact sibling of ``dashboard.py``.
"""

from __future__ import annotations

from importlib.resources import as_file, files

from fastapi import APIRouter
from fastapi.responses import FileResponse

forensics_router = APIRouter()


@forensics_router.get("/forensics", include_in_schema=False)
async def forensics() -> FileResponse:
    """Return the forensics page HTML."""
    asset = files("sidequest.server").joinpath("static/forensics.html")
    with as_file(asset) as path:
        return FileResponse(path, media_type="text/html")
