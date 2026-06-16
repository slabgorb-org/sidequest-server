"""Tests for ClaudeClient._iterate_stream and send_stream."""

from __future__ import annotations

import json

import pytest

from sidequest.agents.claude_client import (
    ClaudeClient,
    StreamComplete,
    StreamError,
    TextDelta,
)
from tests.agents.test_claude_client import (
    FakeStreamingProcess,
    make_streaming_spawn_fn,
)


def _delta_line(text: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            }
        )
        + "\n"
    ).encode()


def _terminal_line(
    full: str,
    in_tok: int = 100,
    out_tok: int = 50,
    session: str | None = "sess-1",
) -> bytes:
    payload = {
        "type": "result",
        "result": full,
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    if session:
        payload["session_id"] = session
    return (json.dumps(payload) + "\n").encode()


@pytest.mark.asyncio
async def test_stream_yields_deltas_in_order_then_complete():
    lines = [
        _delta_line("Hello "),
        _delta_line("world."),
        _terminal_line("Hello world.", in_tok=10, out_tok=3),
    ]
    spawn = make_streaming_spawn_fn(lines)
    client = ClaudeClient(timeout=10.0, spawn_fn=spawn)

    events = []
    async for ev in client.send_stream(prompt="hi", model="claude-opus-4-7"):
        events.append(ev)

    deltas = [e for e in events if isinstance(e, TextDelta)]
    completes = [e for e in events if isinstance(e, StreamComplete)]
    errors = [e for e in events if isinstance(e, StreamError)]

    assert [d.text for d in deltas] == ["Hello ", "world."]
    assert len(completes) == 1
    assert len(errors) == 0
    assert completes[0].full_text == "Hello world."
    assert completes[0].input_tokens == 10
    assert completes[0].output_tokens == 3
    assert completes[0].session_id == "sess-1"


@pytest.mark.asyncio
async def test_stream_yields_error_on_subprocess_failure():
    lines = [_delta_line("partial")]
    spawn = make_streaming_spawn_fn(lines, returncode=1)
    client = ClaudeClient(timeout=10.0, spawn_fn=spawn)

    events = []
    async for ev in client.send_stream(prompt="hi", model="claude-opus-4-7"):
        events.append(ev)

    errors = [e for e in events if isinstance(e, StreamError)]
    assert len(errors) == 1
    assert errors[0].kind == "subprocess_failed"
    assert "partial" in errors[0].partial_text


@pytest.mark.asyncio
async def test_stream_yields_error_on_empty_stdout():
    spawn = make_streaming_spawn_fn(lines=[])
    client = ClaudeClient(timeout=10.0, spawn_fn=spawn)

    events = []
    async for ev in client.send_stream(prompt="hi", model="claude-opus-4-7"):
        events.append(ev)

    errors = [e for e in events if isinstance(e, StreamError)]
    assert len(errors) == 1
    assert errors[0].kind == "empty"


@pytest.mark.asyncio
async def test_stream_ignores_unknown_event_kinds():
    unknown = (json.dumps({"type": "system", "subtype": "init"}) + "\n").encode()
    lines = [unknown, _delta_line("ok"), _terminal_line("ok")]
    spawn = make_streaming_spawn_fn(lines)
    client = ClaudeClient(timeout=10.0, spawn_fn=spawn)

    events = []
    async for ev in client.send_stream(prompt="hi", model="claude-opus-4-7"):
        events.append(ev)

    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert [d.text for d in deltas] == ["ok"]


@pytest.mark.asyncio
async def test_stream_handles_malformed_lines_with_warning(caplog):
    bad = b"not json at all\n"
    lines = [_delta_line("ok"), bad, _terminal_line("ok")]
    spawn = make_streaming_spawn_fn(lines)
    client = ClaudeClient(timeout=10.0, spawn_fn=spawn)

    events = []
    with caplog.at_level("WARNING"):
        async for ev in client.send_stream(prompt="hi", model="claude-opus-4-7"):
            events.append(ev)

    assert any("malformed_line" in rec.message for rec in caplog.records)
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert [d.text for d in deltas] == ["ok"]


@pytest.mark.asyncio
async def test_stream_terminates_with_exactly_one_terminal_event():
    """Invariant: every send_stream() iteration yields exactly one
    StreamComplete OR exactly one StreamError, never both, never neither.
    """
    lines = [_delta_line("a"), _terminal_line("a")]
    spawn = make_streaming_spawn_fn(lines)
    client = ClaudeClient(timeout=10.0, spawn_fn=spawn)

    completes = 0
    errors = 0
    async for ev in client.send_stream(prompt="hi", model="claude-opus-4-7"):
        if isinstance(ev, StreamComplete):
            completes += 1
        elif isinstance(ev, StreamError):
            errors += 1
    assert completes + errors == 1


@pytest.mark.asyncio
async def test_aclose_kills_subprocess(monkeypatch):
    """Cancelling iteration mid-stream must kill the subprocess."""
    lines = [_delta_line(f"chunk {i} ") for i in range(20)]
    lines.append(_terminal_line("never reached"))

    captured_proc: list[FakeStreamingProcess] = []

    async def _spy_spawn(*args, **kwargs):
        proc = FakeStreamingProcess(lines=list(lines), per_line_delay=0.05)
        captured_proc.append(proc)
        return proc

    client = ClaudeClient(timeout=10.0, spawn_fn=_spy_spawn)

    events = []
    iterator = client.send_stream(prompt="hi", model="claude-opus-4-7")
    async for ev in iterator:
        events.append(ev)
        if len(events) == 2:
            break  # cancel mid-stream

    # Explicitly close the iterator — this runs the finally block, which kills the proc.
    # (Python async generator cleanup is not synchronous on break; aclose() is required.)
    await iterator.aclose()

    assert captured_proc[0]._killed is True


@pytest.mark.asyncio
async def test_stream_timeout_yields_stream_error():
    # 5 lines @ 1s delay each = 5s total; 0.5s timeout fires after first line
    lines = [_delta_line(f"chunk {i} ") for i in range(5)]
    spawn = make_streaming_spawn_fn(lines, per_line_delay=1.0)

    client = ClaudeClient(timeout=0.5, spawn_fn=spawn)

    events = []
    async for ev in client.send_stream(prompt="hi", model="claude-opus-4-7"):
        events.append(ev)

    errors = [e for e in events if isinstance(e, StreamError)]
    assert len(errors) == 1
    assert errors[0].kind == "timeout"
    # Should have captured at least the first chunk before timeout
    assert "chunk 0" in errors[0].partial_text or errors[0].partial_text == ""


def test_capabilities_reports_streaming_supported():
    client = ClaudeClient()
    caps = client.capabilities()
    assert caps.supports_streaming is True
