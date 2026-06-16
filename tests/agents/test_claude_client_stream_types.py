"""Tests for streaming event type hierarchy."""

from __future__ import annotations

import dataclasses

import pytest

from sidequest.agents.claude_client import (
    StreamComplete,
    StreamError,
    StreamEvent,
    TextDelta,
)


def test_text_delta_is_stream_event():
    delta = TextDelta(text="hello")
    assert isinstance(delta, StreamEvent)
    assert delta.text == "hello"


def test_text_delta_is_frozen():
    delta = TextDelta(text="hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        # frozen=True dataclass — assignment must raise FrozenInstanceError
        delta.text = "world"  # type: ignore[misc]


def test_stream_complete_carries_usage_metadata():
    done = StreamComplete(
        full_text="full prose",
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=10,
        cache_read_input_tokens=5,
        session_id="abc-123",
        elapsed_seconds=3.14,
    )
    assert isinstance(done, StreamEvent)
    assert done.full_text == "full prose"
    assert done.input_tokens == 100
    assert done.session_id == "abc-123"
    assert done.elapsed_seconds == 3.14


def test_stream_error_carries_failure_detail():
    err = StreamError(
        kind="timeout",
        elapsed_seconds=120.0,
        partial_text="prose got this far",
        detail="claude CLI timed out after 120.0s",
        exit_code=None,
    )
    assert isinstance(err, StreamEvent)
    assert err.kind == "timeout"
    assert err.partial_text == "prose got this far"
