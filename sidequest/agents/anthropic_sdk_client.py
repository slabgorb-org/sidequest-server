"""AnthropicSdkClient — Phase A foundation."""

from __future__ import annotations

import inspect
import logging
import math
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
# event with ``io_fingerprint`` priority (decision C).
#
# Story 61-followup-A — the baseline deques are keyed on ``session_id``
# (``dict[str, deque]``), NOT instance-wide. Deterministic-URL session
# rejoins (``/play/{date}-{world}-mp``) inherit the prior session's
# baseline window; distinct sessions stay independent even when one
# client instance backs several sessions in sequence. Mirrors the
# 61-followup-D ``_session_cumulative_cost_usd: dict[str, float]``
# pattern. Calls with ``session_id=None`` (non-narrator codepaths like
# the dungeon materializer one-shot curate) are detector no-ops — no
# read, no append.

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

# Story 61-followup-D — trained-into-silence mitigations layered onto the
# 61-4 detector surface.
#
# (A) Baseline ceiling: clamp the rolling-mean baseline used in the
# comparator at 3× the warmup floors. The 5×-cost-multiple and
# 2×-I/O-fingerprint rules then have hard upper trip thresholds
# (5×$0.09=$0.45 for cost; 2×36_000=72_000 tokens for input) regardless
# of how high the rolling mean has drifted under sustained runaway. The
# 60-7 annees_folles ramp (11 turns at $0.165 sustained, no 5x alarm)
# is the specific shape this clamp catches on the input axis.
_BASELINE_COST_CEILING: float = 3.0 * _WARMUP_COST_USD_FLOOR
_BASELINE_INPUT_CEILING: int = 3 * _WARMUP_INPUT_TOKENS_FLOOR

# (B) Absolute input_tokens floor: fires regardless of baseline AND
# regardless of output_tokens. Catches the high-output sibling of the
# 60K-in/12-out fingerprint — a 50K-in/800-out call slips past the I/O
# fingerprint (output≥50) but is still a strong canary for snapshot
# bloat / section misroute. Halfway between ~20K healthy steady-state
# and 60K runaway fingerprint (story body §B).
_ABSOLUTE_INPUT_TOKENS_FLOOR: int = 40_000

# (C) Session-cumulative HARD KILL: per-session_id cumulative cost
# capped at $10.00. Sized at ~333 healthy turns ($0.03 target) — generous
# headroom for a real long session, tight enough that a 60-7-class
# regression at $0.165/turn caps at ~60 turns / one playtest evening,
# not a weekend. Overridable via SIDEQUEST_SESSION_COST_CEILING_USD for
# the manual-playtest closure step (operator lowers to $0.50 to validate
# end-to-end termination without running up a real bill).
_SESSION_COST_CEILING_USD: float = 10.0


class AnthropicSdkClientError(LlmClientError):
    """Base error from AnthropicSdkClient."""


class AnthropicSdkConfigError(AnthropicSdkClientError):
    """Construction-time configuration problem (missing key, bad TTL)."""


class AnthropicSdkLoopExceeded(AnthropicSdkClientError):
    """The tool-use loop did not converge within max_iterations."""


class AnthropicSdkCostCeilingExceeded(AnthropicSdkClientError):
    """Per-session cumulative API spend exceeded the configured ceiling.

    Terminal for the session: subsequent calls for the same ``session_id``
    re-raise without making an SDK call. The exception carries the
    session_id, the cumulative figure that crossed the ceiling, and the
    ceiling itself so the WS handler / broadcast layer can build the
    typed ``session.cost_ceiling_exceeded`` message without grovelling
    at strings (Story 61-followup-D §C.2).
    """

    def __init__(
        self,
        message: str,
        *,
        session_id: str,
        cumulative_cost_usd: float,
        ceiling_usd: float,
    ) -> None:
        super().__init__(message)
        self.session_id = session_id
        self.cumulative_cost_usd = cumulative_cost_usd
        self.ceiling_usd = ceiling_usd


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

        # Story 61-followup-A — per-session_id rolling baselines for the
        # cost-runaway fingerprint detector. Two parallel windows
        # (cost_usd, input_tokens), each a K=10 deque, lazily created on
        # first append for a session. ``llm_factory.build_llm_client`` no
        # longer guarantees one-client-per-session post-A: deterministic
        # multiplayer URLs (memory project_session_id_dropin) rejoin the
        # same logical session, and a single client instance may back
        # several sessions across a process lifetime. Keying on
        # session_id makes the baseline window correctly per-session
        # regardless of how the client is reused. Mirrors the
        # ``_session_cumulative_cost_usd`` shape below.
        #
        # Unbounded growth note: the dict gains one entry per distinct
        # session_id seen by the client. The 61-followup-C wiring of
        # ``SessionRoom.close_store()`` → ``reset_baselines(session_id)``
        # will provide the per-session eviction.
        self._cost_baseline: dict[str, deque[float]] = {}
        self._input_tokens_baseline: dict[str, deque[int]] = {}

        # Story 61-followup-D — per-session_id cumulative cost tracker
        # and the configurable ceiling. ``None`` session_ids bypass the
        # tracker (non-narrator codepaths). Env var override is parsed
        # at construction with the same no-silent-fallback discipline as
        # the cache TTL above — non-parseable or non-positive values
        # raise AnthropicSdkConfigError immediately.
        ceiling_env = os.environ.get("SIDEQUEST_SESSION_COST_CEILING_USD")
        if ceiling_env is None:
            self.session_cost_ceiling_usd: float = _SESSION_COST_CEILING_USD
        else:
            try:
                parsed = float(ceiling_env)
            except ValueError as exc:
                raise AnthropicSdkConfigError(
                    f"SIDEQUEST_SESSION_COST_CEILING_USD={ceiling_env!r} "
                    "could not be parsed as a float."
                ) from exc
            # Reject NaN, ±inf, and non-positive values. Python's float()
            # accepts 'inf', 'nan', 'infinity' silently — and NaN comparisons
            # return False so `cumulative >= nan` never fires, which would
            # silently disable the entire hard-kill feature. inf likewise
            # produces an unreachable ceiling. Reviewer 2026-05-23 finding
            # (security + rule-checker §11 + edge-hunter).
            if not math.isfinite(parsed) or parsed <= 0.0:
                raise AnthropicSdkConfigError(
                    f"SIDEQUEST_SESSION_COST_CEILING_USD={ceiling_env!r} "
                    "must be a finite positive number."
                )
            self.session_cost_ceiling_usd = parsed

        # Per-session_id cumulative cost. Populated after every successful
        # call in ``complete_with_tools``; consulted at the next call's
        # entry to short-circuit if the ceiling has already been crossed.
        self._session_cumulative_cost_usd: dict[str, float] = {}
        # Per-session "ceiling already announced" tracker. Prevents
        # duplicate ``session.cost_ceiling_exceeded`` emits when the same
        # session keeps trying to make calls after the kill.
        self._session_ceiling_announced: set[str] = set()

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
        session_id: str | None = None,
    ) -> ToolingResult:
        # Story 61-followup-D §C.2 — pre-flight ceiling check. A session
        # whose cumulative has already crossed the ceiling on a prior call
        # MUST raise immediately without touching the SDK. This is the
        # "no further billing" half of the terminal-refusal contract.
        if session_id is not None:
            self._check_cost_ceiling(session_id)

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
                # floors), fire the watcher event if any trigger matches,
                # then append to the baseline window so subsequent calls
                # compare against PRIOR calls. The entire lifecycle
                # (read → emit → append) lives inside
                # ``_maybe_emit_cost_runaway`` to keep the detector's
                # state management encapsulated. ``session_id=None``
                # bypasses the detector entirely (non-narrator).
                self._maybe_emit_cost_runaway(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    model=response.model,
                    session_id=session_id,
                )

                # Story 61-followup-D §C.2 — per-iter cumulative update +
                # threshold-cross detection. Each iter has already billed;
                # the ceiling cannot un-bill the call that just landed. The
                # check fires the typed event + raises so subsequent iters
                # (and subsequent calls for this session) refuse without
                # touching the SDK.
                if session_id is not None:
                    self._update_session_cumulative(
                        session_id=session_id,
                        cost_usd=cost,
                        model=response.model,
                    )

            text_chunks, tool_use_blocks = self._split_content(response.content)
            text = "".join(text_chunks)
            if on_text_delta is not None and text:
                on_text_delta(text)
            last_text = text or last_text

            if response.stop_reason != "tool_use":
                # Story 61-followup-D §C.3 — per-turn pulse for the GM
                # panel live counter. Fires once per successful turn, not
                # per tool-loop iteration; bypassed when session_id is
                # None (non-narrator paths).
                if session_id is not None:
                    self._emit_cost_running_total(
                        session_id=session_id,
                        model=last_model,
                    )

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

    def reset_baselines(self, session_id: str) -> None:
        """Reset the rolling baselines for a single session.

        Story 61-followup-A: per-session signature. Drops the
        ``cost_usd`` and ``input_tokens`` deques for ``session_id`` so
        the next call cohort for that session uses warmup floors. Other
        sessions' deques are untouched — a session ending must not
        clobber its concurrent neighbors' baseline windows.

        ``session_id`` is required and typed: an accidental
        ``reset_baselines()`` with no argument would silently clear
        nothing (load-bearing for the upcoming 61-followup-C call site).
        A never-observed ``session_id`` is a no-op (``dict.pop`` with
        default ``None``) — important because a session can disconnect
        before making its first SDK call, and the teardown path must
        not crash on that race.

        Load-bearing call site: ``SessionRoom.close_store()``, which
        ``ws_endpoint`` invokes when the last player disconnects (Story
        61-followup-C). The absolute cost floor at
        ``_ABSOLUTE_COST_USD_FLOOR`` and the baseline-ceiling clamp at
        ``_BASELINE_COST_CEILING`` are the live safety nets for the
        trained-into-silence case; this method is the per-session
        eviction handle on top of those nets.

        **Scope:** this method clears ONLY the cost-runaway baselines
        (``_cost_baseline`` and ``_input_tokens_baseline``). The
        61-followup-D state for the same session_id —
        ``_session_cumulative_cost_usd`` and
        ``_session_ceiling_announced`` — is intentionally NOT cleared
        here. A future follow-up (tracked alongside 61-followup-B's
        broader cost-trend telemetry work) should decide whether the
        ``close_store`` eviction path also needs to drop those entries;
        for a slug-recycle rejoin (where the same session_id will be
        reused by a fresh session), the answer is almost certainly
        YES — a stale announce-set entry would silently suppress the
        new session's first ceiling-cross alarm. Deferred so the
        decision and its OTEL plumbing land together.

        Background on why the reset matters: ``RoomRegistry`` (defined
        at session_room.py:817) never evicts a slug today — the
        ``AnthropicSdkClient`` instance backing a slug's orchestrator
        therefore lives for the server process lifetime, not
        per-session. Without this reset, even per-session-keyed
        baselines accumulate entries forever (one per distinct
        session_id seen). ``close_store()`` is the per-session eviction
        handle that gives RoomRegistry's permanent-room model a clean
        per-session baseline-reset surface.
        """
        self._cost_baseline.pop(session_id, None)
        self._input_tokens_baseline.pop(session_id, None)

    def _maybe_emit_cost_runaway(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        model: str,
        session_id: str | None,
    ) -> None:
        """Fire ``cost_runaway_suspected`` if any trigger matches.

        Two parallel rolling baselines compare the just-observed call
        against PRIOR calls (K=10 window each). Story 61-followup-A
        keys both windows on ``session_id``; Story 61-followup-D §A
        clamps the post-warmup baselines at 3× the warmup floors so
        sustained runaways cannot train the comparator into silence:

        - **Cost trigger** (`cost_multiple`): ``cost_usd > 5 ×
          baseline_cost``. Warmup floor: $0.03 → trip $0.15. Post-warmup,
          clamped baseline ≤ $0.09 → trip ≤ $0.45 even if observed mean
          has drifted higher.
        - **I/O fingerprint** (`io_fingerprint`): ``input_tokens > 2 ×
          baseline_input AND output_tokens < 50``. Warmup floor: 12_000
          → trip 24_000. Post-warmup, clamped baseline ≤ 36_000 → trip
          ≤ 72_000 with output < 50.
        - **Absolute input floor** (`input_absolute`, 61-followup-D §B):
          ``input_tokens > 40_000`` ALWAYS fires, regardless of baseline
          AND regardless of output shape. Catches the high-output sibling
          of the I/O fingerprint that snapshot-bloat produces.
        - **Absolute cost floor** (`cost_absolute`, Architect spec-check
          A): ``cost_usd > $0.30`` ALWAYS fires, regardless of baseline.
          Single-call safety net for the trained-into-silence case.
          Baselines also reset on slug recycle — see ``reset_baselines``
          and ``SessionRoom.close_store``.

        ``session_id`` is required (keyword-only). ``session_id=None``
        bypasses the detector entirely — non-narrator codepaths
        (dungeon materializer one-shot curate, future ad-hoc one-shots)
        don't have a session identity and must not pollute any session's
        baseline window. Mirrors the existing None bypass on the
        session-cumulative tracker (see ``_update_session_cumulative``).

        Exactly one event per call: when multiple triggers fire
        simultaneously, priority is **io_fingerprint > input_absolute >
        cost_multiple > cost_absolute** (decision C, extended by
        spec-check A and 61-followup-D §B — the I/O signature is the
        most diagnostic, matching the 2026-05-23 incident exactly;
        input_absolute slots second as the input-axis canary
        independent of output; the cost triggers act as amplifiers).
        All trigger conditions are still surfaced through the
        ``cost_usd`` / ``baseline_cost_usd`` / ``input_tokens`` /
        ``baseline_input_tokens`` field pairs so the operator sees the
        full picture in one event regardless of which trigger named it.
        The published event also carries ``session_id`` so the GM panel
        can attribute interleaved multi-session events (61-followup-A
        addition, TEA finding § Question).
        """
        # Story 61-followup-A — None bypass for non-narrator callers.
        # No silent fallback (CLAUDE.md): the contract is "detector is
        # off for None"; we don't synthesize a magic "<no-session>" key.
        if session_id is None:
            return

        cost_window = self._cost_baseline.get(session_id)
        input_window = self._input_tokens_baseline.get(session_id)
        # Warmup uses floors when EITHER window hasn't accumulated K
        # observations yet. Both windows are populated together at the
        # append site, so they advance in lock-step; the OR is a
        # defensive read against a partial-init race that today can't
        # occur but would be a silent comparator bug if it ever did.
        warmup = (
            cost_window is None or input_window is None or len(cost_window) < _BASELINE_WINDOW_K
        )
        if warmup:
            baseline_cost = _WARMUP_COST_USD_FLOOR
            baseline_input: float = _WARMUP_INPUT_TOKENS_FLOOR
        else:
            # Story 61-followup-D §A — clamp the rolling baseline at the
            # ceiling so the comparator cannot self-train into silence
            # under sustained ramps. The clamp is a CEILING (min, not
            # fixed): healthy steady-state baselines below the ceiling
            # are unchanged; only drifted baselines are clipped down.
            # ``cost_window`` and ``input_window`` are non-None here
            # because warmup is False (asserts on pyright path).
            assert cost_window is not None and input_window is not None
            observed_cost = sum(cost_window) / len(cost_window)
            observed_input = sum(input_window) / len(input_window)
            baseline_cost = min(observed_cost, _BASELINE_COST_CEILING)
            baseline_input = min(observed_input, float(_BASELINE_INPUT_CEILING))

        # Note: cost-multiple trigger erodes if a sustained runaway trains
        # the baseline. The 61-followup-D §A clamp now lowers the trip
        # threshold (5×$0.09=$0.45) against the drifted-baseline case;
        # the absolute floor (>$0.30/call) remains the per-call safety
        # net.
        cost_triggered = cost_usd > _COST_TRIGGER_MULTIPLE * baseline_cost
        io_triggered = (
            input_tokens > _IO_FINGERPRINT_INPUT_MULTIPLE * baseline_input
            and output_tokens < _IO_FINGERPRINT_OUTPUT_CEILING
        )
        # Story 61-followup-D §B — absolute input_tokens floor. Catches
        # the high-output sibling of the 60K-in/12-out fingerprint
        # (50K-in/800-out slips past io_fingerprint at output≥50 but is
        # still a strong canary for snapshot bloat).
        input_absolute_triggered = input_tokens > _ABSOLUTE_INPUT_TOKENS_FLOOR
        # Architect spec-check A: absolute floor — fires regardless of how
        # high the rolling baseline has self-trained. Safety net for the
        # "trained-into-silence" case where a sustained runaway calibrates
        # the rolling baseline upward.
        absolute_triggered = cost_usd > _ABSOLUTE_COST_USD_FLOOR
        any_triggered = (
            cost_triggered or io_triggered or input_absolute_triggered or absolute_triggered
        )

        if any_triggered:
            # Priority order (decision C, extended by 61-followup-D §B):
            # 1. io_fingerprint (most diagnostic; matches 2026-05-23 shape)
            # 2. input_absolute (input-axis canary independent of output)
            # 3. cost_multiple (rolling-baseline-relative; existing trigger)
            # 4. cost_absolute (safety net for trained-into-silence baseline)
            if io_triggered:
                trigger = "io_fingerprint"
            elif input_absolute_triggered:
                trigger = "input_absolute"
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
                # Story 61-followup-A: GM-panel attribution for interleaved
                # multi-session events. session_id is non-None here (the
                # bypass returned early above).
                "session_id": session_id,
            }
            logger.error(
                "narrator.cost_runaway_suspected trigger=%s input=%d "
                "output=%d cost_usd=%.6f baseline_cost_usd=%.6f "
                "baseline_input_tokens=%.1f warmup=%s model=%s "
                "session_id=%s",
                trigger,
                input_tokens,
                output_tokens,
                cost_usd,
                baseline_cost,
                baseline_input,
                warmup,
                model,
                session_id,
            )
            _watcher_publish_event(
                "cost_runaway_suspected",
                fields,
                component="narrator.sdk",
                severity="warn",
            )

        # Story 61-followup-A — append AFTER the check so the comparator
        # always sees PRIOR observations. The append happens whether or
        # not a trigger fired: a healthy call must seed the baseline so
        # the next call has priors. Lazy-init the deques on first
        # observation for this session_id (mirrors the
        # ``_session_cumulative_cost_usd`` dict in the cumulative-cost
        # tracker).
        self._cost_baseline.setdefault(session_id, deque(maxlen=_BASELINE_WINDOW_K)).append(
            cost_usd
        )
        self._input_tokens_baseline.setdefault(session_id, deque(maxlen=_BASELINE_WINDOW_K)).append(
            input_tokens
        )

    # ------------------------------------------------------------------
    # session-cumulative cost ceiling (Story 61-followup-D §C)
    # ------------------------------------------------------------------

    def _build_ceiling_exceeded(
        self,
        *,
        session_id: str,
        cumulative: float,
    ) -> AnthropicSdkCostCeilingExceeded:
        """Construct the typed ceiling-exceeded exception with the
        canonical message + actionable fields. Centralized so the
        three raise sites (pre-flight, already-announced re-raise,
        first-crossing raise) cannot drift in wording or field shape.
        """
        return AnthropicSdkCostCeilingExceeded(
            f"Session {session_id!r} has exceeded its "
            f"${self.session_cost_ceiling_usd:.2f} ceiling "
            f"(cumulative=${cumulative:.4f}).",
            session_id=session_id,
            cumulative_cost_usd=cumulative,
            ceiling_usd=self.session_cost_ceiling_usd,
        )

    def _check_cost_ceiling(self, session_id: str) -> None:
        """Pre-flight check at the entry of ``complete_with_tools``.

        Raises ``AnthropicSdkCostCeilingExceeded`` if the session's
        cumulative has already crossed the ceiling on a prior call.
        Terminal: the announce-set entry from the first crossing keeps
        subsequent refusals silent on the watcher (single emit per
        session), but the exception still re-raises so callers cannot
        accidentally swallow the kill.
        """
        cumulative = self._session_cumulative_cost_usd.get(session_id, 0.0)
        if cumulative >= self.session_cost_ceiling_usd:
            raise self._build_ceiling_exceeded(session_id=session_id, cumulative=cumulative)

    def _update_session_cumulative(
        self,
        *,
        session_id: str,
        cost_usd: float,
        model: str,
    ) -> None:
        """Update the per-session cumulative AFTER a billable iter.

        If the update pushes cumulative across the ceiling, emit the
        typed ``session.cost_ceiling_exceeded`` watcher event (once) and
        raise ``AnthropicSdkCostCeilingExceeded``. The iter that crossed
        has ALREADY billed Anthropic — we cannot un-bill it. The kill
        is "no further calls", not "no further tokens for this call".
        """
        cumulative = self._session_cumulative_cost_usd.get(session_id, 0.0) + cost_usd
        self._session_cumulative_cost_usd[session_id] = cumulative

        if cumulative < self.session_cost_ceiling_usd:
            return
        # Already announced? Then we're in a re-raise path; do not emit
        # again (single emit per session). This branch is only reachable
        # when the same call's later iter crosses AFTER an earlier iter
        # already crossed and the event already fired — should not
        # happen in practice (the loop raises on first cross) but the
        # guard is cheap.
        if session_id in self._session_ceiling_announced:
            raise self._build_ceiling_exceeded(session_id=session_id, cumulative=cumulative)

        logger.error(
            "session.cost_ceiling_exceeded session_id=%s "
            "cumulative_cost_usd=%.6f ceiling_usd=%.2f model=%s",
            session_id,
            cumulative,
            self.session_cost_ceiling_usd,
            model,
        )
        _watcher_publish_event(
            "session.cost_ceiling_exceeded",
            {
                "session_id": session_id,
                "cumulative_cost_usd": cumulative,
                "ceiling_usd": self.session_cost_ceiling_usd,
                "model": model,
            },
            component="narrator.sdk",
            severity="error",
        )
        # State cleanup ordering (lang-review §14): announce-set add
        # MUST be after the side-effecting emit. If _watcher_publish_event
        # raised, an earlier add would poison the announced set and the
        # GM-panel event would be permanently lost on retry. Reviewer
        # 2026-05-23 rule-checker finding.
        self._session_ceiling_announced.add(session_id)
        raise self._build_ceiling_exceeded(session_id=session_id, cumulative=cumulative)

    def _emit_cost_running_total(
        self,
        *,
        session_id: str,
        model: str,
    ) -> None:
        """Per-turn pulse for the GM-panel live counter.

        Fires once per successful ``complete_with_tools`` return (not
        per tool-loop iteration). Severity is ``info`` — routine
        per-turn signal, not an alarm. The fraction_used field is the
        operator's "X / $10" denominator.
        """
        cumulative = self._session_cumulative_cost_usd.get(session_id, 0.0)
        ceiling = self.session_cost_ceiling_usd
        fraction_used = cumulative / ceiling if ceiling > 0.0 else 0.0
        _watcher_publish_event(
            "session.cost_running_total",
            {
                "session_id": session_id,
                "cumulative_cost_usd": cumulative,
                "ceiling_usd": ceiling,
                "fraction_used": fraction_used,
                "model": model,
            },
            component="narrator.sdk",
            severity="info",
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
                    dict(block) if isinstance(block, dict) else block for block in content
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
