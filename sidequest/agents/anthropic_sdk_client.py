"""AnthropicSdkClient — Phase A foundation."""

from __future__ import annotations

import inspect
import logging
import os
from collections import deque
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
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish_event

logger = logging.getLogger(__name__)


# Story 61-4 — Cost-runaway fingerprint detector. Two parallel rolling
# baselines (K=10 each) compare each SDK call against either an observed
# baseline (post-warmup) or warmup floors (pre-warmup) for cost and input
# tokens. Either trigger fires the same ``cost_runaway_suspected`` event,
# distinguished by a ``trigger`` discriminator field. The 60K-in/12-out
# fingerprint from the 2026-05-23 incident hits both; collapse to a single
# event with ``io_fingerprint`` priority (decision C). Per-instance state:
# each session creates a fresh client via ``llm_factory.build_llm_client``,
# so per-instance deques are per-session by construction.

_BASELINE_WINDOW_K: int = 10
_WARMUP_COST_USD_FLOOR: float = 0.03
_WARMUP_INPUT_TOKENS_FLOOR: int = 12_000
_COST_TRIGGER_MULTIPLE: float = 5.0
_IO_FINGERPRINT_INPUT_MULTIPLE: float = 2.0
_IO_FINGERPRINT_OUTPUT_CEILING: int = 50
# Architect spec-check A: absolute ceiling that fires REGARDLESS of the
# rolling baseline. The rolling baseline can self-train onto a sustained
# runaway — if 10 consecutive turns bill at $0.12 each, the baseline
# averages to ~$0.12 and call 11 at $0.18 is only 1.5x baseline (sub-5x
# threshold → silent). The May-23 incident, if it had run continuously,
# would have calibrated the alarm into silence within 10 calls. The
# absolute floor at $0.30/call (10x the $0.03 healthy-turn target) is the
# safety net for that case. Symmetric with the I/O fingerprint trigger,
# which already has an absolute output<50 floor.
_ABSOLUTE_COST_USD_FLOOR: float = 0.30


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
        # turn. A 1h write is 2x base and is INTENDED to amortize across
        # an ~85-turn session. Operators can still opt back to 5m via the
        # env var.
        #
        # Story 60-4 (2026-05-23): the 1h amortization is now realized on
        # tool-use continuations as well. ``complete_with_tools`` adds a moving
        # cache_control breakpoint on the last content block of the newest
        # continuation message, so the appended tool_use/tool_result blocks
        # ride the same cache as system_blocks[0] + tools instead of forcing a
        # 5m re-mint of the ~11.7k prefix on every iter 2+. Measured savings
        # vs the original bug shape: ~70% of per-turn narrator cost (60-3
        # baseline ~$0.116/turn → ~$0.035/turn post-fix). See
        # ``sprint/archive/60-3-session.md`` (diagnosis) and
        # ``sprint/archive/60-4-session.md`` (fix).
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

        # Story 61-4 — per-instance rolling baselines for the cost-runaway
        # fingerprint detector. Two parallel windows (cost_usd, input_tokens)
        # each bounded to K=10 observations; pre-warmup comparisons use the
        # locked floors above. ``llm_factory.build_llm_client`` returns a fresh
        # client per session, making per-instance state per-session by
        # construction.
        self._cost_baseline: deque[float] = deque(maxlen=_BASELINE_WINDOW_K)
        self._input_tokens_baseline: deque[int] = deque(maxlen=_BASELINE_WINDOW_K)

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

        initial_message_count = len(running_messages)

        for iteration in range(1, max_iterations + 1):
            # Story 60-4: on continuation calls (iter 2+, where running_messages
            # has been extended past the input set with appended tool_use /
            # tool_result blocks), build the API payload with a moving
            # cache_control breakpoint on the LAST content block of the newest
            # user (tool_result) message. Without this marker the API re-mints
            # the ~11.7k system_blocks[0]+tools prefix at the default 5m TTL on
            # every continuation regardless of the system/tools markers, since
            # the appended messages sit AFTER those markers in cache-prefix
            # order. Measured cost: ~$0.089/turn wasted cache_write
            # (sprint/archive/60-3-session.md). The payload is rebuilt fresh
            # per iteration so prior calls' captured kwargs stay snapshot-clean.
            payload_messages = self._build_messages_payload(
                running_messages,
                is_continuation=len(running_messages) > initial_message_count,
            )
            with llm_request_span(model=model, iteration=iteration) as span:
                response = await self._sdk.messages.create(
                    model=model,
                    system=sdk_system,
                    messages=payload_messages,
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

                # Story 60-4: pass the per-TTL split so 1h writes are billed at
                # the real 2x base rate instead of being aggregated into the 5m
                # rate (compute_cost_usd previously had a single cache-write
                # field that defaulted to the 5m rate). Fallback: if the SDK
                # didn't expose the nested cache_creation breakdown (anthropic
                # < 0.51) bill the aggregate at the 5m rate, matching the
                # historical pricing behavior — no silent under-billing.
                cost_kwargs: dict[str, Any] = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cached_input_read_tokens": cache_read,
                    "model": response.model,
                }
                if cache_creation is not None:
                    cost_kwargs["cached_input_write_5m_tokens"] = cache_write_5m
                    cost_kwargs["cached_input_write_1h_tokens"] = cache_write_1h
                else:
                    cost_kwargs["cached_input_write_tokens"] = cache_write
                cost = compute_cost_usd(**cost_kwargs)
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

                # Story 61-4 — Cost-runaway fingerprint detector. Check the
                # just-observed call against the rolling baselines (or warmup
                # floors) BEFORE appending the call to those baselines, so the
                # comparison is always against PRIOR calls. See module-level
                # constants above for the locked thresholds (decisions A-C).
                self._maybe_emit_cost_runaway(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    model=response.model,
                )
                self._cost_baseline.append(cost)
                self._input_tokens_baseline.append(input_tokens)

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
    # cost-runaway fingerprint alarm (Story 61-4)
    # ------------------------------------------------------------------

    def reset_baselines(self) -> None:
        """Clear both rolling baselines so the next call uses warmup floors.

        Architect spec-check A: ``RoomRegistry`` (session_room.py:774-786)
        never evicts a slug — the ``AnthropicSdkClient`` instance backing
        a slug's orchestrator therefore lives for the server process
        lifetime, not per-session. Without this reset, the rolling
        baseline can self-train onto a sustained runaway: 10 consecutive
        $0.12 turns calibrate the baseline to ~$0.12, after which an
        $0.18 follow-up at 1.5x baseline is below the 5x trigger and
        passes silently. Call this from the slug-recycle path
        (``SessionRoom.close_store``) so the next session starts cold.
        The absolute floor at $0.30/call (``_ABSOLUTE_COST_USD_FLOOR``)
        is the in-session safety net for the same trained-into-silence
        failure mode.
        """
        self._cost_baseline.clear()
        self._input_tokens_baseline.clear()

    def _maybe_emit_cost_runaway(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        model: str,
    ) -> None:
        """Fire ``cost_runaway_suspected`` if any trigger matches.

        Two parallel rolling baselines compare the just-observed call
        against PRIOR calls (K=10 window each):

        - **Cost trigger**: ``cost_usd > 5 × baseline_cost`` (warmup
          floor: $0.03 → trip threshold $0.15).
        - **I/O fingerprint trigger**: ``input_tokens > 2 × baseline_input
          AND output_tokens < 50`` (warmup floor: 12_000 → trip threshold
          24_000 with output < 50).
        - **Absolute cost floor** (Architect spec-check A): ``cost_usd >
          $0.30`` ALWAYS fires, regardless of baseline. The rolling
          baseline can self-train onto a sustained runaway (10 turns at
          $0.12 → baseline averages ~$0.12 → next $0.18 turn is sub-5x
          and silent). The absolute floor at 10x the $0.03 healthy-turn
          target is the safety net. Symmetric with the I/O fingerprint
          trigger, which already has an absolute output<50 floor.
          Baselines also reset on slug recycle — see ``reset_baselines``
          and ``SessionRoom.close_store``.

        Exactly one event per call: when multiple triggers fire
        simultaneously, priority is io_fingerprint > cost_multiple >
        cost_absolute (decision C, extended by spec-check A — the I/O
        signature is the most diagnostic, matching the 2026-05-23
        incident exactly; the cost_multiple condition is still surfaced
        through the ``cost_usd`` / ``baseline_cost_usd`` field pair so
        operators see the full picture in one event).
        """
        warmup = len(self._cost_baseline) < _BASELINE_WINDOW_K
        if warmup:
            baseline_cost = _WARMUP_COST_USD_FLOOR
            baseline_input = _WARMUP_INPUT_TOKENS_FLOOR
        else:
            baseline_cost = sum(self._cost_baseline) / len(self._cost_baseline)
            baseline_input = sum(self._input_tokens_baseline) / len(
                self._input_tokens_baseline
            )

        cost_triggered = cost_usd > _COST_TRIGGER_MULTIPLE * baseline_cost
        io_triggered = (
            input_tokens > _IO_FINGERPRINT_INPUT_MULTIPLE * baseline_input
            and output_tokens < _IO_FINGERPRINT_OUTPUT_CEILING
        )
        # Architect spec-check A: absolute floor — fires regardless of how
        # high the rolling baseline has self-trained. Safety net for the
        # "trained-into-silence" case where a sustained runaway calibrates
        # the rolling baseline upward.
        absolute_triggered = cost_usd > _ABSOLUTE_COST_USD_FLOOR
        if not (cost_triggered or io_triggered or absolute_triggered):
            return

        # Priority order (decision C, extended by spec-check A):
        # 1. io_fingerprint (most diagnostic; matches 2026-05-23 shape)
        # 2. cost_multiple (rolling-baseline-relative; existing trigger)
        # 3. cost_absolute (safety net for trained-into-silence baseline)
        if io_triggered:
            trigger = "io_fingerprint"
        elif cost_triggered:
            trigger = "cost_multiple"
        else:
            trigger = "cost_absolute"
        fields: dict[str, Any] = {
            "trigger": trigger,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "baseline_cost_usd": baseline_cost,
            "baseline_input_tokens": baseline_input,
            "warmup": warmup,
            "model": model,
        }
        logger.error(
            "narrator.cost_runaway_suspected trigger=%s input=%d output=%d "
            "cost_usd=%.6f baseline_cost_usd=%.6f baseline_input_tokens=%.1f "
            "warmup=%s",
            trigger,
            input_tokens,
            output_tokens,
            cost_usd,
            baseline_cost,
            baseline_input,
            warmup,
        )
        _watcher_publish_event(
            "cost_runaway_suspected",
            fields,
            component="narrator.sdk",
            severity="warn",
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _build_messages_payload(
        self,
        running_messages: list[dict[str, Any]],
        *,
        is_continuation: bool,
    ) -> list[dict[str, Any]]:
        """Build the ``messages`` array for a single ``messages.create`` call.

        Story 60-4: on continuation calls (iter 2+, where the tool-use loop has
        appended ``{role:'assistant', tool_use}`` + ``{role:'user', tool_result}``
        to ``running_messages``), the LAST content block of the newest user
        message gets a ``cache_control={'type':'ephemeral', 'ttl':self.cache_ttl}``
        marker. This covers the appended messages under the same cache, so the
        API stops re-minting the ~11.7k ``system_blocks[0]+tools`` prefix at the
        default 5m TTL on every continuation (measured root cause in
        ``sprint/archive/60-3-session.md``).

        Each call returns a fresh list of fresh message dicts (content blocks
        are copied where they're dicts). Two reasons:

        1. **Snapshot semantics.** The Anthropic SDK doesn't mutate the kwargs
           we hand it, but observers (tests, OTEL middleware) may capture them
           by reference. A fresh per-iteration payload guarantees prior calls'
           captured kwargs reflect what was actually sent then, not what the
           in-place mutation looks like now.
        2. **Stale-marker cleanup.** Earlier continuations marked their own
           newest user message; this iteration's marker must be on the *new*
           newest message, with prior message-level markers cleared. Building
           fresh achieves the cleanup without mutating shared state.

        For non-continuation calls (iter 1 with no appended tool turns), no
        marker is added — the initial user message rides the cached system
        prefix without needing its own breakpoint.
        """
        out: list[dict[str, Any]] = []
        for msg in running_messages:
            new_msg: dict[str, Any] = {"role": msg["role"]}
            content = msg.get("content")
            if isinstance(content, list):
                new_msg["content"] = [
                    dict(block) if isinstance(block, dict) else block
                    for block in content
                ]
            else:
                new_msg["content"] = content
            out.append(new_msg)

        if not is_continuation or not out:
            return out

        # Mark the last content block of the newest message (the freshly
        # appended tool_result user pair). Skip when content is a bare string
        # (no block-level addressable structure) or empty — both are non-
        # continuation shapes that shouldn't happen here but degrade safely.
        last_msg = out[-1]
        last_content = last_msg.get("content")
        if isinstance(last_content, list) and last_content:
            last_block = last_content[-1]
            if isinstance(last_block, dict):
                last_block["cache_control"] = {
                    "type": "ephemeral",
                    "ttl": self.cache_ttl,
                }

        return out

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
        # ~7.6K tokens, no per-turn drift. A marker on the last entry requests
        # caching of the whole tools array at the configured TTL (1h by
        # default). See ADR-101 four-region cache layout amendment.
        #
        # Story 60-4 (2026-05-23): the continuation-append site in
        # complete_with_tools now adds a moving cache_control breakpoint on
        # the newest tool_result message, which covers the appended messages
        # under the same cache and unlocks the 1h rebate this marker promised
        # in isolation. Together with system_blocks[0]'s marker, both halves
        # of the cached prefix now rebate on continuation calls.
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
