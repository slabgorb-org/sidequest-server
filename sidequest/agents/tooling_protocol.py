"""Typed surface for tooling-capable LLM backends (ADR-101 successor of ADR-001).

The narrator orchestrator targets ToolingLlmClient.complete_with_tools.
Narrow LlmClient (ClaudeClient, OllamaClient) handles auxiliary text-only paths.
The split is intentional — Ollama cannot serve cached tool round-trips and
should not be reachable from a narrator path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CacheableBlock:
    """A system-prompt segment that may carry an Anthropic cache_control marker."""

    text: str
    cache: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    """A user/assistant message in the conversation messages array."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """JSON-Schema-described tool the model may call."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """A single tool invocation emitted by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """The handler's reply to a ToolUseBlock, fed back to the model."""

    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ToolingResult:
    """Final outcome of a `complete_with_tools` call after the tool loop settles."""

    text: str
    stop_reason: Literal["end_turn", "max_tokens", "stop_sequence", "tool_use", "error"]
    input_tokens: int
    output_tokens: int
    cached_input_read_tokens: int
    cached_input_write_tokens: int
    model: str
    tool_calls: list[ToolUseBlock] = field(default_factory=list)
    # Sum of `compute_cost_usd` across every iteration of the tool-use
    # loop. Defaults to 0.0 so legacy test doubles that construct
    # ToolingResult by hand keep working — production paths populate it.
    cumulative_cost_usd: float = 0.0
    # Per-TTL breakdown of `cached_input_write_tokens` from
    # `usage.cache_creation.ephemeral_{5m,1h}_input_tokens`. Default to 0
    # for SDK-version drift (<0.51 didn't expose the breakdown) and for
    # legacy fixtures.
    cached_input_write_5m_tokens: int = 0
    cached_input_write_1h_tokens: int = 0


# Handler signature the registry exposes to the client.
# (args_json, tool_use_id) -> ToolResultBlock
ToolHandler = Callable[[dict[str, Any], str], "ToolResultBlock"]


@runtime_checkable
class ToolingLlmClient(Protocol):
    """Tooling-capable LLM client — extends the narrow LlmClient surface.

    Phase A defines the surface only. Phase B's tool_registry wires real
    handlers; Phase C populates `tools` with the 26-tool v1 catalog.
    """

    async def complete_with_tools(
        self,
        system_blocks: list[CacheableBlock],
        messages: list[Message],
        tools: list[ToolDefinition],
        tool_dispatch: Callable[[ToolUseBlock], Awaitable[ToolResultBlock] | ToolResultBlock]
        | None = None,
        *,
        model: str,
        max_iterations: int = 8,
        max_tokens: int = 4096,
        on_text_delta: Callable[[str], None] | None = None,
        session_id: str | None = None,
    ) -> ToolingResult:
        """Drive one narrator turn through the tool-use loop.

        ``session_id`` (Story 61-followup-D §C.1): opaque session
        identifier forwarded from ``TurnContext.session_id``. The
        Anthropic SDK backend keys per-session cumulative cost against
        this value to enforce the $10 hard kill. Pass ``None`` for
        non-narrator codepaths (``claude -p`` curate, tests without a
        session) to bypass the per-session tracker entirely — do NOT
        substitute a sentinel string, which would corrupt the tracker
        by sharing a single key across unrelated callers. Backends
        that do not track per-session cost accept and ignore the kwarg.
        """
        ...
