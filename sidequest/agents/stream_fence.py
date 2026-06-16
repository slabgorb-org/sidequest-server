"""StreamFenceParser — splits a streaming claude response into prose deltas
and a buffered game_patch JSON block.

Driven externally — feed() called per TextDelta from claude_client.send_stream;
finalize() called once after stream EOS. Prose chunks emit via the
on_prose_delta callback as soon as they're confirmed not to be part of a
fence; JSON is accumulated internally.

Boundary patterns are LABEL-AWARE — only the literal ``\n```game_patch`` opener
and ``\n``` `` closer match. Non-game_patch fences in prose (```python, bare ```)
pass through unchanged.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

# CRLF-tolerant patterns. Open requires the literal "game_patch" label;
# close is bare. The leading `\r?\n` anchors the fence to a line boundary.
_OPEN_FENCE = re.compile(r"\r?\n```game_patch[ \t]*\r?\n")
# During streaming (feed), close fence must be followed by \r?\n — the `$`
# alternative is NOT included here because it would match mid-stream when a
# chunk ends exactly at the closing backticks, causing the next `\n` chunk to
# be misidentified as trailing garbage.
_CLOSE_FENCE_STREAM = re.compile(r"\r?\n```[ \t]*\r?\n")
# At finalize (EOS) we also accept a fence with no trailing newline via `$`.
_CLOSE_FENCE_FINAL = re.compile(r"\r?\n```[ \t]*(?:\r?\n|$)")

# Lookahead size: enough bytes to recognize "\n```game_patch\n" if a chunk
# boundary cuts the fence in half. The longest prefix-of-fence we might need
# to hold back is len("\r\n```game_patch") = 16 bytes.
_LOOKAHEAD_BYTES = 16


class _State(enum.Enum):
    PROSE = "prose"
    JSON_BUFFERING = "json_buffering"
    EPILOGUE = "epilogue"


@dataclass(frozen=True, slots=True)
class FenceParseResult:
    prose: str
    game_patch_json: str | None
    status: Literal["complete", "no_fence", "unclosed_fence", "trailing_garbage"]
    fence_offset: int | None  # offset in concatenated stream where the open fence appeared


class StreamFenceParser:
    """Splits a streaming claude response into prose (live) and game_patch (buffered).

    Driven externally — feed() called per TextDelta, finalize() called at EOS.
    Prose chunks are emitted via the on_prose_delta callback as soon as they
    are confirmed to not be part of a fence. JSON is accumulated internally.
    """

    def __init__(
        self,
        on_prose_delta: Callable[[str], Awaitable[None]],
        on_fence_detected: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._on_prose_delta = on_prose_delta
        self._on_fence_detected = on_fence_detected
        self._state: _State = _State.PROSE
        self._carry: str = ""
        self._json_buffer: str = ""
        self._prose_total: str = ""
        self._fence_offset: int | None = None
        self._epilogue_garbage: bool = False
        self._finalized: bool = False
        self._stream_offset: int = 0

    async def feed(self, chunk: str) -> None:
        if self._finalized:
            raise RuntimeError("StreamFenceParser.feed() called after finalize()")

        self._carry += chunk

        # Drive the state machine until it stops making progress on this carry.
        while True:
            if self._state is _State.PROSE:
                progressed = await self._handle_prose()
                if not progressed:
                    return
            elif self._state is _State.JSON_BUFFERING:
                progressed = self._handle_json()
                if not progressed:
                    return
            else:  # EPILOGUE
                # Discard everything in carry as trailing garbage.
                if self._carry:
                    self._epilogue_garbage = True
                    self._carry = ""
                return

    async def _handle_prose(self) -> bool:
        """Returns True if state changed (loop should re-enter)."""
        match = _OPEN_FENCE.search(self._carry)
        if match is not None:
            prefix = self._carry[: match.start()]
            if prefix:
                await self._on_prose_delta(prefix)
                self._prose_total += prefix
            self._fence_offset = self._stream_offset + match.start()
            self._stream_offset += match.end()
            self._carry = self._carry[match.end() :]
            self._state = _State.JSON_BUFFERING
            if self._on_fence_detected is not None:
                await self._on_fence_detected(len(self._prose_total))
            return True

        # No confirmed fence. Hold back a lookahead-sized tail.
        safe_emit_len = max(0, len(self._carry) - _LOOKAHEAD_BYTES)
        if safe_emit_len > 0:
            emit = self._carry[:safe_emit_len]
            self._carry = self._carry[safe_emit_len:]
            self._stream_offset += safe_emit_len
            await self._on_prose_delta(emit)
            self._prose_total += emit
        return False

    def _handle_json(self, *, final: bool = False) -> bool:
        pattern = _CLOSE_FENCE_FINAL if final else _CLOSE_FENCE_STREAM
        match = pattern.search(self._carry)
        if match is not None:
            self._json_buffer += self._carry[: match.start()]
            self._stream_offset += match.end()
            self._carry = self._carry[match.end() :]
            self._state = _State.EPILOGUE
            return True

        # Hold back lookahead-sized tail in case it's the start of a close fence.
        safe_buffer_len = max(0, len(self._carry) - _LOOKAHEAD_BYTES)
        if safe_buffer_len > 0:
            self._json_buffer += self._carry[:safe_buffer_len]
            self._carry = self._carry[safe_buffer_len:]
            self._stream_offset += safe_buffer_len
        return False

    async def finalize(self) -> FenceParseResult:
        if self._finalized:
            raise RuntimeError("finalize() called twice")
        self._finalized = True

        # Flush remaining carry per terminal state.
        if self._state is _State.PROSE:
            if self._carry:
                await self._on_prose_delta(self._carry)
                self._prose_total += self._carry
                self._carry = ""
            return FenceParseResult(
                prose=self._prose_total,
                game_patch_json=None,
                status="no_fence",
                fence_offset=None,
            )

        if self._state is _State.JSON_BUFFERING:
            # Try final close-fence match (allows $ as terminator for no-trailing-newline).
            self._handle_json(final=True)
            if self._state is _State.EPILOGUE:
                # Close fence found — fall through to epilogue handling below.
                if self._carry:
                    self._epilogue_garbage = True
                    self._carry = ""
                status: Literal["complete", "trailing_garbage"] = (
                    "trailing_garbage" if self._epilogue_garbage else "complete"
                )
                return FenceParseResult(
                    prose=self._prose_total,
                    game_patch_json=self._json_buffer,
                    status=status,
                    fence_offset=self._fence_offset,
                )
            # Still no close fence — genuinely unclosed.
            self._json_buffer += self._carry
            self._carry = ""
            return FenceParseResult(
                prose=self._prose_total,
                game_patch_json=self._json_buffer,
                status="unclosed_fence",
                fence_offset=self._fence_offset,
            )

        # EPILOGUE
        if self._carry:
            self._epilogue_garbage = True
            self._carry = ""
        epilogue_status: Literal["complete", "trailing_garbage"] = (
            "trailing_garbage" if self._epilogue_garbage else "complete"
        )
        return FenceParseResult(
            prose=self._prose_total,
            game_patch_json=self._json_buffer,
            status=epilogue_status,
            fence_offset=self._fence_offset,
        )
