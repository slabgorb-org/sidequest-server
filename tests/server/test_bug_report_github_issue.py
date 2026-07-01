"""Task 7 — GitHub issue creation (RED)."""
from __future__ import annotations

import httpx
import pytest

from sidequest.server.github_issue import GitHubIssueError, create_issue


@pytest.mark.asyncio
async def test_create_issue_returns_url_and_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_CI_TOKEN", "tok")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/7", "number": 7})

    out = await create_issue("T", "B", ["bug"], transport=httpx.MockTransport(handler))
    assert out == {"url": "https://github.com/o/r/issues/7", "number": 7}
    assert captured["url"] == "https://api.github.com/repos/slabgorb-org/sidequest/issues"
    assert captured["auth"] == "Bearer tok"


@pytest.mark.asyncio
async def test_create_issue_non_2xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_CI_TOKEN", "tok")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(GitHubIssueError):
        await create_issue("T", "B", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_create_issue_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_CI_TOKEN", raising=False)
    with pytest.raises(GitHubIssueError):
        await create_issue("T", "B")


@pytest.mark.asyncio
async def test_create_issue_malformed_2xx_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_CI_TOKEN", "tok")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"unexpected": "shape"})

    with pytest.raises(GitHubIssueError):
        await create_issue("T", "B", transport=httpx.MockTransport(handler))
