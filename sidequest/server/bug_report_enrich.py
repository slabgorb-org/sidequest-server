"""Enrichment helpers for the in-app bug reporter: secret scrubbing, server-log
tail, OTEL summary, and Markdown issue-body composition.

The target repo is PUBLIC, so every log/OTEL string passes through ``scrub``
before it is embedded in an issue. ``scrub`` only *removes* — it never rewrites
meaning — so applying it defensively is always safe.
"""
from __future__ import annotations

import os
import re
from collections import deque
from pathlib import Path

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
