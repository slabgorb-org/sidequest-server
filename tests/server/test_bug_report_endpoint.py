"""Task 8 — POST /api/bug-report wiring (RED)."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub
from tests._helpers.doubles import FakeSocket


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", "postgresql://localhost/test_notreal")
    from sidequest.server.app import create_app

    return TestClient(create_app(), raise_server_exceptions=True)


@pytest.fixture
async def bound_hub() -> WatcherHub:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


def _patch_backends(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import sidequest.server.bug_report as bug_report

    captured: dict[str, Any] = {"uploads": []}

    def fake_upload(key: str, data: bytes, content_type: str) -> str:
        captured["uploads"].append((key, content_type))
        return f"https://cdn.slabgorb.com/{key}"

    async def fake_create_issue(title, body, labels=None, **kw):  # noqa: ANN001, ARG001
        captured["title"] = title
        captured["body"] = body
        return {"url": "https://github.com/slabgorb-org/sidequest/issues/99", "number": 99}

    monkeypatch.setattr(bug_report, "upload_bytes", fake_upload)
    monkeypatch.setattr(bug_report, "create_issue", fake_create_issue)
    return captured


def test_bug_report_route_is_registered(app_client: TestClient) -> None:
    # Missing required fields → 422, proving the route exists (not 404).
    resp = app_client.post("/api/bug-report", data={})
    assert resp.status_code == 422, f"route must be registered; got {resp.status_code}"


def test_bug_report_happy_path_returns_issue_url(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _patch_backends(monkeypatch)
    resp = app_client.post(
        "/api/bug-report",
        data={"title": "Broken dice", "description": "Dice never settle", "session_slug": ""},
        files=[("files", ("shot.png", b"\x89PNG\r\n", "image/png"))],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["issue_url"] == "https://github.com/slabgorb-org/sidequest/issues/99"
    assert body["issue_number"] == 99
    assert "report_id" in body
    assert len(captured["uploads"]) == 1
    assert "![shot.png]" in captured["body"]


@pytest.mark.asyncio
async def test_bug_report_emits_watcher_event(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", "postgresql://localhost/test_notreal")
    _patch_backends(monkeypatch)
    from sidequest.server.app import create_app

    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]

    client = TestClient(create_app(), raise_server_exceptions=True)
    resp = client.post(
        "/api/bug-report",
        data={"title": "t", "description": "d", "session_slug": "s1"},
    )
    assert resp.status_code == 201, resp.text
    await asyncio.sleep(0.05)

    created = [e for e in sock.events if e.get("event_type") == "bug_report.created"]
    assert created, f"expected bug_report.created; got {[e.get('event_type') for e in sock.events]}"
    assert created[0]["fields"]["issue_number"] == 99
    assert created[0]["component"] == "bug_report"


def test_bug_report_too_many_files_rejected(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_backends(monkeypatch)
    files = [("files", (f"s{i}.png", b"x", "image/png")) for i in range(7)]
    resp = app_client.post(
        "/api/bug-report",
        data={"title": "t", "description": "d"},
        files=files,
    )
    assert resp.status_code == 400


def test_bug_report_r2_failure_aborts_502(app_client, monkeypatch):
    import sidequest.server.bug_report as bug_report
    from sidequest.server.r2_upload import R2UploadError

    def boom_upload(key, data, content_type):
        raise R2UploadError("r2 down")

    async def must_not_run(*a, **k):
        raise AssertionError("create_issue must not be called after an R2 failure")

    monkeypatch.setattr(bug_report, "upload_bytes", boom_upload)
    monkeypatch.setattr(bug_report, "create_issue", must_not_run)
    resp = app_client.post(
        "/api/bug-report",
        data={"title": "t", "description": "d"},
        files=[("files", ("s.png", b"x", "image/png"))],
    )
    assert resp.status_code == 502


def test_bug_report_github_failure_aborts_502(app_client, monkeypatch):
    import sidequest.server.bug_report as bug_report
    from sidequest.server.github_issue import GitHubIssueError

    def fake_upload(key, data, content_type):
        return f"https://cdn.slabgorb.com/{key}"

    async def boom_issue(*a, **k):
        raise GitHubIssueError("gh down")

    monkeypatch.setattr(bug_report, "upload_bytes", fake_upload)
    monkeypatch.setattr(bug_report, "create_issue", boom_issue)
    resp = app_client.post(
        "/api/bug-report",
        data={"title": "t", "description": "d"},
    )
    assert resp.status_code == 502


def test_bug_report_oversized_file_rejected_400(app_client, monkeypatch):
    import sidequest.server.bug_report as bug_report

    _patch_backends(monkeypatch)
    monkeypatch.setattr(bug_report, "MAX_FILE_BYTES", 10)
    resp = app_client.post(
        "/api/bug-report",
        data={"title": "t", "description": "d"},
        files=[("files", ("big.png", b"x" * 50, "image/png"))],
    )
    assert resp.status_code == 400


def test_bug_report_wrong_type_rejected_400(app_client, monkeypatch):
    _patch_backends(monkeypatch)
    resp = app_client.post(
        "/api/bug-report",
        data={"title": "t", "description": "d"},
        files=[("files", ("evil.exe", b"x", "application/octet-stream"))],
    )
    assert resp.status_code == 400
