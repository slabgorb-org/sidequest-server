"""Tests for NDJSON event parsing helpers."""

from __future__ import annotations

from sidequest.agents.claude_stream_parser import (
    extract_terminal_metadata,
    extract_text_delta,
    is_terminal_event,
)

# Sample event shapes — verified against `claude -p --output-format stream-json`
# during implementation. These are placeholders updated in Task 3 with
# captured fixture data.

DELTA_EVENT = {
    "type": "assistant",
    "message": {
        "content": [{"type": "text", "text": "Hello "}],
    },
}

TERMINAL_EVENT = {
    "type": "result",
    "result": "Hello world.",
    "usage": {
        "input_tokens": 100,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    },
    "session_id": "sess-abc",
}

UNKNOWN_EVENT = {"type": "system", "subtype": "init"}


def test_extract_text_delta_returns_chunk_for_assistant_event():
    chunk = extract_text_delta(DELTA_EVENT)
    assert chunk == "Hello "


def test_extract_text_delta_returns_none_for_non_assistant():
    assert extract_text_delta(TERMINAL_EVENT) is None
    assert extract_text_delta(UNKNOWN_EVENT) is None


def test_is_terminal_event_recognizes_result_type():
    assert is_terminal_event(TERMINAL_EVENT) is True


def test_is_terminal_event_returns_false_for_non_terminal():
    assert is_terminal_event(DELTA_EVENT) is False
    assert is_terminal_event(UNKNOWN_EVENT) is False


def test_extract_terminal_metadata_pulls_usage_and_session():
    meta = extract_terminal_metadata(TERMINAL_EVENT)
    assert meta.full_text == "Hello world."
    assert meta.input_tokens == 100
    assert meta.output_tokens == 5
    assert meta.session_id == "sess-abc"


def test_extract_terminal_metadata_handles_missing_usage():
    minimal = {"type": "result", "result": "ok"}
    meta = extract_terminal_metadata(minimal)
    assert meta.full_text == "ok"
    assert meta.input_tokens is None
    assert meta.output_tokens is None
    assert meta.session_id is None


# ---------------------------------------------------------------------------
# Fixture-driven integration tests — locked against real `claude -p --output-format stream-json --verbose` output
# ---------------------------------------------------------------------------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream_sample.ndjson"


def _load_events() -> list[dict]:
    events = []
    with FIXTURE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip non-JSON lines (e.g., stderr leaked into stdout)
                continue
    return events


def test_real_fixture_yields_at_least_one_text_delta():
    events = _load_events()
    deltas = [extract_text_delta(e) for e in events]
    text_chunks = [d for d in deltas if d is not None]
    assert len(text_chunks) > 0
    # Concatenated chunks should contain "hello" (case-insensitive)
    assert "hello" in "".join(text_chunks).lower()


def test_real_fixture_has_exactly_one_terminal_event():
    events = _load_events()
    terminals = [e for e in events if is_terminal_event(e)]
    assert len(terminals) == 1


def test_real_fixture_terminal_metadata_has_usage():
    events = _load_events()
    [terminal] = [e for e in events if is_terminal_event(e)]
    meta = extract_terminal_metadata(terminal)
    assert meta.input_tokens is not None
    assert meta.output_tokens is not None
    assert meta.full_text != ""


# ---------------------------------------------------------------------------
# Edge-case tests flagged by prior reviewer
# ---------------------------------------------------------------------------


def test_extract_text_delta_joins_multiple_text_blocks():
    multi_block = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world."},
            ],
        },
    }
    assert extract_text_delta(multi_block) == "Hello world."


def test_extract_text_delta_returns_none_for_content_with_no_text_blocks():
    tool_only = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": "x", "name": "y"}]},
    }
    assert extract_text_delta(tool_only) is None


def test_extract_terminal_metadata_handles_missing_result_field():
    no_result = {"type": "result"}
    meta = extract_terminal_metadata(no_result)
    assert meta.full_text == ""
