"""Enrichment helpers for the in-app bug reporter: secret scrubbing, server-log
tail, OTEL summary, and Markdown issue-body composition.

The target repo is PUBLIC, so every log/OTEL string passes through ``scrub``
before it is embedded in an issue. ``scrub`` only *removes* — it never rewrites
meaning — so applying it defensively is always safe.
"""
from __future__ import annotations

import json
import os
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sidequest.telemetry.watcher_hub import watcher_hub

LOG_TAIL_LINES = 200
OTEL_EVENT_LIMIT = 150
GITHUB_BODY_LIMIT = 65536
_LOG_BLOCK_MAX = 16000
_OTEL_BLOCK_MAX = 16000
_TRUNC = "\n…(truncated)"
_REDACTED = "«redacted»"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"gh[posru]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"),
)
_ENV_SECRET_VARS = (
    "SIDEQUEST_CI_TOKEN",
    "R2_SECRET_ACCESS_KEY",
    "R2_ACCESS_KEY_ID",
    "ANTHROPIC_API_KEY",
)


def scrub(text: str) -> str:
    """Redact secret-shaped tokens, known secret env-var literals, and rewrite
    the absolute home dir to ``~``. Removal-only; safe to over-apply."""
    if not text:
        return text
    out = text
    for var in _ENV_SECRET_VARS:
        val = os.environ.get(var)
        if val and len(val) >= 8:
            out = out.replace(val, _REDACTED)
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_REDACTED, out)
    home = str(Path.home())
    if home and home != "/":
        out = out.replace(home, "~")
    return out


def server_log_path() -> Path:
    override = os.environ.get("SIDEQUEST_SERVER_LOG")
    if override:
        return Path(override)
    return Path.home() / ".sidequest" / "logs" / "sidequest-server.log"


def tail_server_log(n_lines: int = LOG_TAIL_LINES) -> str | None:
    """Last ``n_lines`` of the server log, or ``None`` when the file is absent
    or unreadable. Absence is recorded loudly by the caller — never faked."""
    path = server_log_path()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = deque(fh, maxlen=n_lines)
    except OSError:
        return None
    return "".join(lines)


async def otel_summary(slug: str, limit: int = OTEL_EVENT_LIMIT) -> str | None:
    """Compact one-line-per-event OTEL summary for ``slug`` (last ``limit``
    events), or ``None`` when there is no active session or nothing buffered."""
    if not slug:
        return None
    events = await watcher_hub.buffered_events(slug)
    if not events:
        return None
    lines: list[str] = []
    for e in events[-limit:]:
        ts = e.get("timestamp", "")
        sev = e.get("severity", "info")
        comp = e.get("component", "")
        et = e.get("event_type", "")
        try:
            fstr = json.dumps(e.get("fields", {}), default=str)
        except (TypeError, ValueError):
            fstr = str(e.get("fields", {}))
        if len(fstr) > 240:
            fstr = fstr[:240] + "…"
        lines.append(f"{ts} [{sev}] {comp} :: {et} {fstr}")
    return "\n".join(lines)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - len(_TRUNC))] + _TRUNC


def _context_table(session_slug: str, context: dict[str, Any]) -> str:
    rows = [
        ("Session", session_slug or "—"),
        ("Genre", context.get("genre") or "—"),
        ("World", context.get("world") or "—"),
        ("Screen", context.get("screen") or "—"),
        ("Build", context.get("appBuild") or context.get("build") or "—"),
        ("Viewport", context.get("viewport") or "—"),
        ("Path", context.get("pathname") or "—"),
        ("User agent", context.get("userAgent") or "—"),
        ("Filed at", datetime.now(UTC).isoformat()),
    ]
    out = "| Field | Value |\n| --- | --- |\n"
    for key, val in rows:
        safe = str(val).replace("|", "\\|")
        out += f"| {key} | {safe} |\n"
    return out


def compose_body(
    *,
    description: str,
    context: dict[str, Any],
    attachments: list[tuple[str, str, bool]],
    log_text: str | None,
    otel_text: str | None,
    report_id: str,
    session_slug: str,
) -> str:
    """Assemble the Markdown issue body. Enrichment absence is written
    explicitly; the whole body is bounded to ``GITHUB_BODY_LIMIT`` with a loud
    truncation marker."""
    parts: list[str] = [description.strip(), "\n\n## Context\n\n" + _context_table(session_slug, context)]

    if attachments:
        parts.append("\n## Attachments\n")
        for name, url, is_image in attachments:
            parts.append(f"![{name}]({url})" if is_image else f"[{name}]({url})")

    parts.append("\n\n## Server log\n")
    if log_text is None:
        parts.append(f"_server log not found at `{scrub(str(server_log_path()))}`_")
    else:
        parts.append(
            f"<details><summary>Server log (scrubbed, last {LOG_TAIL_LINES} lines)</summary>\n\n"
            f"```\n{_truncate(log_text, _LOG_BLOCK_MAX)}\n```\n</details>"
        )

    parts.append("\n\n## OTEL\n")
    if otel_text is None:
        parts.append("_no active session — no OTEL captured_")
    else:
        parts.append(
            f"<details><summary>OTEL — session {session_slug} (scrubbed, last {OTEL_EVENT_LIMIT} events)</summary>\n\n"
            f"```\n{_truncate(otel_text, _OTEL_BLOCK_MAX)}\n```\n</details>"
        )

    parts.append(f"\n\n---\n_report_id: `{report_id}` · Filed from the in-app bug reporter._")

    body = "\n".join(parts)
    if len(body) > GITHUB_BODY_LIMIT:
        body = _truncate(body, GITHUB_BODY_LIMIT)
    return body
