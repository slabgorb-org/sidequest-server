"""AnthropicSdkClient — Phase A foundation."""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.claude_client import LlmClientError
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolDefinition,
    ToolingResult,
    ToolResultBlock,
    ToolUseBlock,
)
from sidequest.telemetry.spans.llm_request import llm_request_span

logger = logging.getLogger(__name__)


class AnthropicSdkClientError(LlmClientError):
    """Base error from AnthropicSdkClient."""


class AnthropicSdkConfigError(AnthropicSdkClientError):
    """Construction-time configuration problem (missing key, bad TTL)."""


class AnthropicSdkLoopExceeded(AnthropicSdkClientError):
    """The tool-use loop did not converge within max_iterations."""


CacheTtl = Literal["5m", "1h"]
_VALID_TTLS: frozenset[str] = frozenset({"5m", "1h"})


# 1h ephemeral cache is a beta: without this header on the request the
# API rejects ``ttl: "1h"`` and every narration turn 400s. Sent only on
# the 1h path — see ``complete_with_tools``.
_EXTENDED_CACHE_TTL_BETA = "extended-cache-ttl-2025-04-11"


class AnthropicSdkClient:
    """Anthropic SDK client implementing ToolingLlmClient."""

    def __init__(
        self,
        *,
        sdk: Any | None = None,
        cache_ttl: CacheTtl | None = None,
    ) -> None:
        self._api_key = os.environ.get("ANTHROPIC_API_KEY")
        if sdk is None and not self._api_key:
            raise AnthropicSdkConfigError(
                "ANTHROPIC_API_KEY not set — required to construct "
                "AnthropicSdkClient without an explicit sdk= injection."
            )

        # Operative default is 1h: submit-and-wait MP cadence routinely
        # exceeds the 5m window, so a 5m write is re-paid almost every
        # turn. A 1h write is 2x base but amortizes across an ~85-turn
        # session. Operators can still opt back to 5m via the env var.
        resolved_ttl = (
            cache_ttl
            if cache_ttl is not None
            else os.environ.get("SIDEQUEST_ANTHROPIC_CACHE_TTL", "1h")
        )
        if resolved_ttl not in _VALID_TTLS:
            raise AnthropicSdkConfigError(
                f"SIDEQUEST_ANTHROPIC_CACHE_TTL={resolved_ttl!r} invalid; "
                f"must be one of {sorted(_VALID_TTLS)}"
            )
        self.cache_ttl: CacheTtl = resolved_ttl  # type: ignore[assignment]

        if sdk is None:
            from anthropic import AsyncAnthropic

            sdk = AsyncAnthropic(api_key=self._api_key)
        self._sdk = sdk

    @property
    def api_key_present(self) -> bool:
        return bool(self._api_key)

    # ------------------------------------------------------------------
    # complete_with_tools
    # ------------------------------------------------------------------

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
    ) -> ToolingResult:
        sdk_system = self._build_system_array(system_blocks)
        sdk_tools = self._build_tools_array(tools)

        running_messages: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content} for m in messages
        ]
        all_tool_uses: list[ToolUseBlock] = []
        last_text = ""
        cumulative_in = 0
        cumulative_out = 0
        cumulative_cache_read = 0
        cumulative_cache_write = 0
        cumulative_cache_write_5m = 0
        cumulative_cache_write_1h = 0
        cumulative_cost_usd = 0.0
        last_model = model

        # ttl:"1h" on the cache_control markers is rejected unless the
        # extended-cache-ttl beta is opted in via this header. The 5m path
        # sends no extra header (request stays identical to the prior
        # behavior). No silent fallback: if the API still rejects 1h the
        # error surfaces, it is not downgraded to 5m.
        extra_headers = (
            {"anthropic-beta": _EXTENDED_CACHE_TTL_BETA} if self.cache_ttl == "1h" else None
        )

        for iteration in range(1, max_iterations + 1):
            with llm_request_span(model=model, iteration=iteration) as span:
                response = await self._sdk.messages.create(
                    model=model,
                    system=sdk_system,
                    messages=running_messages,
                    tools=sdk_tools,
                    max_tokens=max_tokens,
                    extra_headers=extra_headers,
                )
                usage = response.usage
                input_tokens = int(getattr(usage, "input_tokens", 0))
                output_tokens = int(getattr(usage, "output_tokens", 0))
                cache_read = int(getattr(usage, "cache_read_input_tokens", 0))
                cache_write = int(getattr(usage, "cache_creation_input_tokens", 0))
                # Per-TTL breakdown — exposed by anthropic-python>=0.51 via the
                # nested cache_creation object. Older SDKs return no nested
                # object; we keep aggregate-only behavior and report 0 for the
                # breakdown so the operator can see "SDK doesn't expose it"
                # rather than guessing.
                cache_creation = getattr(usage, "cache_creation", None)
                cache_write_5m = (
                    int(getattr(cache_creation, "ephemeral_5m_input_tokens", 0))
                    if cache_creation
                    else 0
                )
                cache_write_1h = (
                    int(getattr(cache_creation, "ephemeral_1h_input_tokens", 0))
                    if cache_creation
                    else 0
                )
                cumulative_in += input_tokens
                cumulative_out += output_tokens
                cumulative_cache_read += cache_read
                cumulative_cache_write += cache_write
                cumulative_cache_write_5m += cache_write_5m
                cumulative_cache_write_1h += cache_write_1h
                last_model = response.model

                cost = compute_cost_usd(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_input_read_tokens=cache_read,
                    cached_input_write_tokens=cache_write,
                    model=response.model,
                )
                cumulative_cost_usd += cost
                span.set_attributes(
                    {
                        "llm.input_tokens": input_tokens,
                        "llm.output_tokens": output_tokens,
                        "llm.cached_input_read_tokens": cache_read,
                        "llm.cached_input_write_tokens": cache_write,
                        "llm.stop_reason": response.stop_reason,
                        "llm.cost_usd": cost,
                    }
                )
                # Per-iter ledger to /tmp/sidequest-server.log so cache
                # hit/miss is visible without a WS tap (Task B3).
                logger.info(
                    "narrator.sdk.usage iter=%d input=%d output=%d "
                    "cache_read=%d cache_write=%d 5m=%d 1h=%d cost_usd=%.6f",
                    iteration,
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_write,
                    cache_write_5m,
                    cache_write_1h,
                    cost,
                )

            text_chunks, tool_use_blocks = self._split_content(response.content)
            text = "".join(text_chunks)
            if on_text_delta is not None and text:
                on_text_delta(text)
            last_text = text or last_text

            if response.stop_reason != "tool_use":
                return ToolingResult(
                    text=last_text,
                    stop_reason=response.stop_reason,
                    input_tokens=cumulative_in,
                    output_tokens=cumulative_out,
                    cached_input_read_tokens=cumulative_cache_read,
                    cached_input_write_tokens=cumulative_cache_write,
                    model=last_model,
                    tool_calls=all_tool_uses,
                    cumulative_cost_usd=cumulative_cost_usd,
                    cached_input_write_5m_tokens=cumulative_cache_write_5m,
                    cached_input_write_1h_tokens=cumulative_cache_write_1h,
                )

            if tool_dispatch is None:
                raise AnthropicSdkClientError(
                    "Model emitted tool_use but no tool_dispatch was provided."
                )

            assistant_blocks: list[dict[str, Any]] = []
            user_results: list[dict[str, Any]] = []
            for tu in tool_use_blocks:
                all_tool_uses.append(tu)
                assistant_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tu.id,
                        "name": tu.name,
                        "input": tu.arguments,
                    }
                )
                maybe = tool_dispatch(tu)
                if inspect.isawaitable(maybe):
                    result = await maybe
                else:
                    result = maybe
                user_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result.tool_use_id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                )
            running_messages = running_messages + [
                {"role": "assistant", "content": assistant_blocks},
                {"role": "user", "content": user_results},
            ]

        raise AnthropicSdkLoopExceeded(
            f"Tool-use loop did not converge in {max_iterations} iterations"
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _build_system_array(self, system_blocks: list[CacheableBlock]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for block in system_blocks:
            entry: dict[str, Any] = {"type": "text", "text": block.text}
            if block.cache:
                # Echo the configured TTL unconditionally — no special-
                # casing. Both "5m" and "1h" are valid cache_control TTLs;
                # the 1h path additionally rides the beta header sent in
                # complete_with_tools.
                entry["cache_control"] = {"type": "ephemeral", "ttl": self.cache_ttl}
            out.append(entry)
        return out

    def _build_tools_array(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]
        # The tools array is byte-stable across every turn — 27 definitions,
        # ~7.6K tokens, no per-turn drift. Without an explicit cache_control
        # marker, Anthropic auto-caches it at default 5m TTL and re-writes
        # the whole block every time the 5m timer expires (which the
        # submit-and-wait MP cadence routinely outlives). A marker on the
        # last entry caches the whole tools array at the configured TTL
        # (1h by default). See ADR-101 four-region cache layout amendment.
        if out:
            out[-1]["cache_control"] = {"type": "ephemeral", "ttl": self.cache_ttl}
        return out

    @staticmethod
    def _split_content(
        content: list[Any],
    ) -> tuple[list[str], list[ToolUseBlock]]:
        text_chunks: list[str] = []
        tool_uses: list[ToolUseBlock] = []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_chunks.append(block.text)
            elif block_type == "tool_use":
                tool_uses.append(
                    ToolUseBlock(
                        id=block.id,
                        name=block.name,
                        arguments=block.input,
                    )
                )
        return text_chunks, tool_uses
