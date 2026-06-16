"""Tests for the /dashboard route — relocates the OTEL dashboard from
``scripts/playtest_dashboard.py`` into the sidequest-server FastAPI app.
"""

from __future__ import annotations

from importlib.resources import files

from fastapi.testclient import TestClient

from sidequest.server.app import create_app


def test_dashboard_html_ships_with_package() -> None:
    """The dashboard HTML must live inside the ``sidequest.server``
    package so it is included in the wheel. If this fails, the file
    was added under the wrong directory or the hatchling include
    config drifted.
    """
    asset = files("sidequest.server").joinpath("static/dashboard.html")
    assert asset.is_file(), f"dashboard.html missing from package: {asset}"


def test_dashboard_route_returns_html() -> None:
    """``GET /dashboard`` must return the dashboard HTML directly from
    the FastAPI app (no separate proxy server)."""
    client = TestClient(create_app())
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>SideQuest OTEL Dashboard</title>" in response.text


def test_dashboard_html_connects_to_ws_watcher() -> None:
    """Regression guard: the embedded JS must open its WebSocket against
    ``/ws/watcher`` (the FastAPI watcher endpoint), not ``/ws`` (the old
    proxy path). If a future edit to the static asset changes this,
    fail loudly here rather than silently breaking the dashboard.
    """
    client = TestClient(create_app())
    response = client.get("/dashboard")
    assert "${proto}//${location.host}/ws/watcher`" in response.text
    assert "${proto}//${location.host}/ws`" not in response.text
