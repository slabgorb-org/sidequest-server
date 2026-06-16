"""Pure helpers for parsing one NDJSON line from `claude -p --output-format stream-json`.

The Claude CLI emits one JSON object per stdout line during streaming. These
helpers classify a parsed event and extract the fields the streaming consumer
in claude_client.py needs.

Field paths are CLI-version-dependent and verified against captured fixture
data in tests. If the CLI changes shape, only this module needs adjustment.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerminalMetadata:
    """Extracted from the final `result` event in a stream."""

    full_text: str
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    session_id: str | None


def extract_text_delta(event: dict) -> str | None:
    """Return the prose chunk from an assistant-message event, or None.

    Returns None for events that don't carry text deltas (terminal events,
    system events, anything unknown).
    """
    if event.get("type") != "assistant":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
    if not chunks:
        return None
    return "".join(chunks)


def is_terminal_event(event: dict) -> bool:
    """True when the event is the stream's final result event."""
    return event.get("type") == "result"


def extract_terminal_metadata(event: dict) -> TerminalMetadata:
    """Pull usage, session_id, and full_text from the terminal result event."""
    full_text = event.get("result")
    if not isinstance(full_text, str):
        full_text = ""

    usage = event.get("usage") if isinstance(event.get("usage"), dict) else None

    def _opt_int(key: str) -> int | None:
        if usage is None:
            return None
        v = usage.get(key)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    session_id = event.get("session_id")
    if not isinstance(session_id, str):
        session_id = None

    return TerminalMetadata(
        full_text=full_text,
        input_tokens=_opt_int("input_tokens"),
        output_tokens=_opt_int("output_tokens"),
        cache_creation_input_tokens=_opt_int("cache_creation_input_tokens"),
        cache_read_input_tokens=_opt_int("cache_read_input_tokens"),
        session_id=session_id,
    )
