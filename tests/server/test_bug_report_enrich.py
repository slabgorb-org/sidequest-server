"""Tasks 2–5 — bug_report_enrich (RED)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.server.bug_report_enrich import scrub


def test_scrub_redacts_token_shapes() -> None:
    text = (
        "pat=github_pat_11ABCDEF0123456789 gho=gho_abcdefghijklmnopqrstuvwxyz012345 "
        "anth=sk-ant-api03-abcDEF_-123 auth: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig "
        "akia=AKIA1234567890ABCDEF"
    )
    out = scrub(text)
    assert "github_pat_11ABCDEF0123456789" not in out
    assert "gho_abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "sk-ant-api03-abcDEF_-123" not in out
    assert "AKIA1234567890ABCDEF" not in out
    assert "Bearer eyJhbGciOiJIUzI1NiJ9" not in out


def test_scrub_redacts_known_env_secret_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_CI_TOKEN", "supersecretvalue12345")
    out = scrub("the token is supersecretvalue12345 ok")
    assert "supersecretvalue12345" not in out


def test_scrub_rewrites_home_path() -> None:
    home = str(Path.home())
    out = scrub(f"reading {home}/.sidequest/logs/x.log")
    assert home not in out
    assert "~/.sidequest/logs/x.log" in out


def test_tail_server_log_returns_last_n_lines(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sidequest.server.bug_report_enrich import tail_server_log

    log = tmp_path / "server.log"
    log.write_text("".join(f"line {i}\n" for i in range(500)), encoding="utf-8")
    monkeypatch.setenv("SIDEQUEST_SERVER_LOG", str(log))

    out = tail_server_log(n_lines=10)
    assert out is not None
    lines = out.splitlines()
    assert len(lines) == 10
    assert lines[-1] == "line 499"
    assert lines[0] == "line 490"


def test_tail_server_log_missing_file_returns_none(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sidequest.server.bug_report_enrich import tail_server_log

    monkeypatch.setenv("SIDEQUEST_SERVER_LOG", str(tmp_path / "does-not-exist.log"))
    assert tail_server_log() is None


@pytest.mark.asyncio
async def test_otel_summary_formats_events(monkeypatch: pytest.MonkeyPatch) -> None:
    import sidequest.server.bug_report_enrich as enrich

    async def fake_buffered(slug):  # noqa: ARG001
        return [
            {"timestamp": "T1", "severity": "info", "component": "turn",
             "event_type": "turn_complete", "fields": {"round": 3}},
        ]

    monkeypatch.setattr(enrich.watcher_hub, "buffered_events", fake_buffered)
    out = await enrich.otel_summary("s1")
    assert out is not None
    assert "turn_complete" in out
    assert "turn" in out
    assert '"round": 3' in out or "'round': 3" in out


@pytest.mark.asyncio
async def test_otel_summary_empty_slug_returns_none() -> None:
    from sidequest.server.bug_report_enrich import otel_summary

    assert await otel_summary("") is None


@pytest.mark.asyncio
async def test_otel_summary_empty_buffer_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import sidequest.server.bug_report_enrich as enrich

    async def fake_buffered(slug):  # noqa: ARG001
        return []

    monkeypatch.setattr(enrich.watcher_hub, "buffered_events", fake_buffered)
    assert await enrich.otel_summary("s1") is None
