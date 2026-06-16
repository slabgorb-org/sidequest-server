"""HTTP route that serves the OTEL dashboard HTML.

The dashboard is a single self-contained HTML file (with embedded CSS
and JavaScript) shipped under ``sidequest/server/static/``. The browser
loads it from this route and opens its own WebSocket against
``/ws/watcher`` on the same origin. There is no separate proxy server.
"""

from __future__ import annotations

from importlib.resources import as_file, files

from fastapi import APIRouter
from fastapi.responses import FileResponse

dashboard_router = APIRouter()


@dashboard_router.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    """Return the dashboard HTML."""
    asset = files("sidequest.server").joinpath("static/dashboard.html")
    with as_file(asset) as path:
        return FileResponse(path, media_type="text/html")
