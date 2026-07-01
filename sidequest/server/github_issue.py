"""Create a GitHub issue on the public tracker via the REST API.

The PAT (``SIDEQUEST_CI_TOKEN``) stays server-side. Issue creation is a hard
dependency: any failure raises ``GitHubIssueError`` and the endpoint maps it to
HTTP 502 — we never silently drop a filed report.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

GITHUB_OWNER = "slabgorb-org"
GITHUB_REPO = "sidequest"
DEFAULT_LABELS: list[str] = ["bug", "in-app-report"]


class GitHubIssueError(RuntimeError):
    """Issue creation failed (missing token, network error, or non-2xx)."""


async def create_issue(
    title: str,
    body: str,
    labels: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    token = os.environ.get("SIDEQUEST_CI_TOKEN")
    if not token:
        raise GitHubIssueError("SIDEQUEST_CI_TOKEN is not set")

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "labels": labels or list(DEFAULT_LABELS)}
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise GitHubIssueError(f"request failed: {exc}") from exc

    if resp.status_code not in (200, 201):
        raise GitHubIssueError(f"{resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    return {"url": data["html_url"], "number": data["number"]}
