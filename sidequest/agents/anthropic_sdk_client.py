"""AnthropicSdkClient — Phase A foundation."""

from __future__ import annotations

import inspect
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from sidequest.agents import cost_safety
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
from sidequest.telemetry.spans.narrator import (
    narrator_tool_loop_cap_hit_span,
    narrator_tool_loop_span,
)
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
#
# Story 91-4 — the constants, the trigger comparator, the ceiling-env
# parsing, and the cross-call-site ledger moved to
# ``sidequest.agents.cost_safety`` so the Haiku adapters (``_AsideLlm``,
# ``_IntentRouterLlm``) run the SAME detector and feed the SAME
# per-session cumulative. Re-bound here (assignment, not import-as) for
# the pre-91-4 importers — semantics and values unchanged.

_BASELINE_WINDOW_K = cost_safety._BASELINE_WINDOW_K
_WARMUP_COST_USD_FLOOR = cost_safety._WARMUP_COST_USD_FLOOR
_WARMUP_INPUT_TOKENS_FLOOR = cost_safety._WARMUP_INPUT_TOKENS_FLOOR
_COST_TRIGGER_MULTIPLE = cost_safety._COST_TRIGGER_MULTIPLE
_IO_FINGERPRINT_INPUT_MULTIPLE = cost_safety._IO_FINGERPRINT_INPUT_MULTIPLE
_IO_FINGERPRINT_OUTPUT_CEILING = cost_safety._IO_FINGERPRINT_OUTPUT_CEILING
_ABSOLUTE_COST_USD_FLOOR = cost_safety._ABSOLUTE_COST_USD_FLOOR
_BASELINE_COST_CEILING = cost_safety._BASELINE_COST_CEILING
_BASELINE_INPUT_CEILING = cost_safety._BASELINE_INPUT_CEILING
_ABSOLUTE_INPUT_TOKENS_FLOOR = cost_safety._ABSOLUTE_INPUT_TOKENS_FLOOR
_SESSION_COST_CEILING_USD = cost_safety._SESSION_COST_CEILING_USD


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


# Story 61-19 — the cache tier for VOLATILE (changes-every-turn) content.
# The per-turn message tail (player action + tool_result deltas, and the
# valley/recency system content that rides between the stable-prefix
# breakpoint and the message breakpoint) is rewritten every turn and read
# back at most once — within the same turn's tool loop, seconds later. The
# 1h tier's 2x write premium only pays off when content persists and is
# re-read across turns; volatile content has zero cross-turn value, so it
# rides the 5m tier (1.25x), which still covers the within-turn read while
# never paying the 1h premium. The STABLE system prefix + tools keep
# ``self.cache_ttl`` (1h by default) — they amortize across turns. See
# session 894 forensics in ``sprint/context/context-story-61-19.md``.
_VOLATILE_CACHE_TTL: CacheTtl = "5m"


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
        # Story 60-4 (2026-05-23): ``complete_with_tools`` adds a moving
        # cache_control breakpoint on the last content block of the newest
        # continuation message, so the appended tool_use/tool_result blocks
        # stop forcing a 5m re-mint of the ~11.7k prefix on every iter 2+.
        # The marker's PRESENCE is what prevents the re-mint (60-3 diagnosis);
        # 60-4 originally set its TTL to ``self.cache_ttl`` (1h).
        #
        # Story 61-19 (2026-05-30): that message-level marker (iter=1 tail AND
        # continuation) moved to ``_VOLATILE_CACHE_TTL`` (5m) — the tail is
        # volatile, so 1h's 2x premium was wasted on it (~9.7k tok/turn, ~73%
        # of session cost, session 894). Only the marker's TTL changed; its
        # presence still prevents the prefix re-mint. The STABLE system prefix
        # (``system_blocks[0]``) + tools keep ``self.cache_ttl`` (1h) and still
        # amortize across turns — an empirical probe confirmed warm turns read
        # the prefix at 1h (write=0) while only the tail writes at 5m. The
        # original 60-4 "~70% savings" figure was measured under the pre-61-19
        # 1h-everywhere layout. See ``sprint/archive/60-3-session.md`` +
        # ``sprint/archive/60-4-session.md`` and
        # ``sprint/context/context-story-61-19.md``.
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
            # Story 91-1: route through the single SDK construction seam.
            # Function-level import dodges the llm_factory → this-module
            # circular import; attribute access (not ``from … import``) keeps
            # the lookup late-bound so the wiring test's monkeypatched fake
            # is what this client receives. An explicit ``sdk=`` injection
            # (the fake-SDK test fleet) never consults the seam.
            from sidequest.agents import llm_factory

            sdk = llm_factory.build_async_anthropic()
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
        # at construction with no-silent-fallback discipline — the
        # validation (NaN/inf/non-positive → AnthropicSdkConfigError,
        # Reviewer 2026-05-23 finding) moved to
        # ``cost_safety.parse_session_cost_ceiling_usd`` (91-4) so the
        # Haiku adapters apply the identical check at THEIR construction.
        self.session_cost_ceiling_usd: float = cost_safety.parse_session_cost_ceiling_usd()

        # Per-session_id cumulative cost + announce set. Story 91-4:
        # these are ALIASES onto the process-level ``SessionCostLedger``
        # dicts — narrator, aside, and intent-router spend all land in
        # ONE pot per session, so the running-total pulse reports the
        # combined figure and a ceiling kill on any call site terminally
        # refuses every other call site for that session. The instance
        # attrs are kept (rather than reading the ledger inline) because
        # the 61-followup-D machinery and its test suite address them
        # here; aliasing changes sharing, not behavior.
        _ledger = cost_safety.ledger()
        self._session_cumulative_cost_usd: dict[str, float] = _ledger.cumulative_cost_usd
        self._session_ceiling_announced: set[str] = _ledger.ceiling_announced

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
        iteration_cap: int | None = None,
        max_tokens: int = 4096,
        on_text_delta: Callable[[str], Awaitable[None] | None] | None = None,
        session_id: str | None = None,
        caller: str = "narrator",
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

        # Story 71-40: the soft ``iteration_cap`` cap-hit span fires at most once
        # per turn — a warning the turn is unusually tool-heavy, not a stop.
        cap_hit_fired = False

        for iteration in range(1, max_iterations + 1):
            # Story 71-40: a turn that reaches the soft cap (set below the hard
            # ``max_iterations`` ceiling) records ONE cap-hit span so the GM panel
            # surfaces the throttled turn. The loop is NOT stopped here — the
            # fail-loud ``AnthropicSdkLoopExceeded`` ceiling below is unchanged.
            if iteration_cap is not None and iteration >= iteration_cap and not cap_hit_fired:
                cap_hit_fired = True
                with narrator_tool_loop_cap_hit_span(
                    iteration_cap=iteration_cap,
                    iterations_used=iteration,
                    max_iterations=max_iterations,
                ):
                    pass
            # Story 60-4/60-7: every iter — iter=1 included — build the API
            # payload with a moving cache_control breakpoint on the LAST
            # content block of the newest user message. Without this marker
            # the API auto-caches the content sitting past our last explicit
            # breakpoint (the system_blocks[0]+tools prefix) at the default 5m
            # TTL — on iter=1 that's the new user message + recency-zone
            # deltas, on iter=2+ it's the appended tool_use / tool_result
            # blocks. The marker pins that volatile tail to a known tier.
            #
            # Story 61-19 (TTL correction): the volatile-tail marker now resolves
            # to ``_VOLATILE_CACHE_TTL`` (5m, 1.25x), NOT the client's 1h tier.
            # The tail changes every turn, so a 1h (2x) write on it is invalidated
            # after a single within-turn read — pure cross-turn waste; 5m covers
            # the seconds-long tool loop at 1.25x. Only the stable system prefix
            # (``system_blocks[0]``) + tools keep 1h, where they amortize.
            # Story 61-20 (volume): the session-static AVAILABLE CULTURES roster
            # and magic hard_limits are zone-promoted INTO that 1h prefix, so the
            # recurring 5m tail shrinks toward the per-turn delta only.
            #
            # The payload is rebuilt fresh per iteration so prior calls' captured
            # kwargs stay snapshot-clean.
            payload_messages = self._build_messages_payload(
                running_messages,
                is_continuation=len(running_messages) > initial_message_count,
            )
            with llm_request_span(model=model, iteration=iteration) as span:
                if on_text_delta is not None:
                    # Story 71-23: solo narration streaming. When a delta sink
                    # is wired, route through ``messages.stream`` so prose ships
                    # token-by-token (each ``text_delta`` event) instead of one
                    # whole block at end-of-iteration. The completed message
                    # (content + usage + stop_reason + model) comes off
                    # ``get_final_message()`` so all downstream cost/tool-loop
                    # logic below is unchanged. The sink may be sync or async
                    # (the orchestrator's sink awaits ``broadcast_delta``).
                    async with self._sdk.messages.stream(
                        model=model,
                        system=sdk_system,
                        messages=payload_messages,
                        tools=sdk_tools,
                        max_tokens=max_tokens,
                        extra_headers=extra_headers,
                    ) as stream:
                        async for event in stream:
                            if getattr(event, "type", None) != "content_block_delta":
                                continue
                            delta = getattr(event, "delta", None)
                            if delta is None or getattr(delta, "type", None) != "text_delta":
                                continue
                            piece = getattr(delta, "text", "")
                            if not piece:
                                continue
                            maybe = on_text_delta(piece)
                            if inspect.isawaitable(maybe):
                                await maybe
                        response = await stream.get_final_message()
                else:
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
                        # Story 91-1: caller tag on every llm.request span so
                        # span-based attribution can split narrator from
                        # dungeon-curate (and any future tool-caller) traffic.
                        "llm.caller": caller,
                        "llm.input_tokens": input_tokens,
                        "llm.output_tokens": output_tokens,
                        "llm.cached_input_read_tokens": cache_read,
                        "llm.cached_input_write_tokens": cache_write,
                        "llm.stop_reason": response.stop_reason,
                        "llm.cost_usd": cost,
                    }
                )
                # Per-iter ledger to the server log so cache hit/miss is
                # visible without a WS tap (Task B3). Story 91-1: the line
                # carries ``caller`` and ``model`` so log-based cost
                # accounting (the /sq-llm-costs Layer-1 reconciliation) can
                # attribute every call — pre-91-1 it was caller- and
                # model-blind.
                logger.info(
                    "narrator.sdk.usage caller=%s model=%s iter=%d input=%d "
                    "output=%d cache_read=%d cache_write=%d 5m=%d 1h=%d "
                    "cost_usd=%.6f",
                    caller,
                    response.model,
                    iteration,
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_write,
                    cache_write_5m,
                    cache_write_1h,
                    cost,
                )

                # Story 61-followup-B — Promote the per-call usage line above
                # to a watcher event so the GM panel has a continuous,
                # plottable per-call cost baseline (the log line is invisible
                # to the watcher transport). severity=info: this is the steady
                # baseline the 61-4 warn alarm and the followup-D session
                # ceiling compare against, not an alarm itself. Fires per SDK
                # call / tool-loop iteration, mirroring the 60-7 cache event's
                # component+shape. cumulative session totals remain
                # followup-D's job (session.cost_running_total).
                _watcher_publish_event(
                    "narrator.sdk.usage",
                    {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost_usd": cost,
                        "model": response.model,
                        "cache_read_tokens": cache_read,
                        "cache_write_tokens": cache_write,
                    },
                    component="narrator.sdk",
                    severity="info",
                )

                # Story 60-7 — Lie-detector for the cache_control regression
                # class: a single iter writing to BOTH tiers at once.
                # Post-61-19 the tiers are split by content — the stable
                # system prefix + tools are the only 1h-marked content, the
                # volatile message tail the only 5m-marked content. So a
                # healthy WARM iter writes 5m-only (tail) with 1h=0 (prefix is
                # a read); a COLD/warmup iter may legitimately write both (1h
                # prefix mint + 5m tail). Both > 0 in a STEADY-STATE iter means
                # the same content is being written to two tiers (e.g. a tail
                # marker defaulting to 5m while a 1h marker covers overlapping
                # content) — the waste pattern 60-7 eliminated. Fires per
                # offending iter (not aggregated per turn) so the GM panel can
                # pin which iteration leaks.
                #
                # Story 91-6 — Gate the WARN on `cache_read > 0`. A cold start
                # (cache_read == 0) has nothing to read back, so a dual write
                # is the unavoidable first mint of both tiers — not the churn
                # pathology. The genuine signal is the conjunction: the prefix
                # IS being read back (cache_read > 0) yet the 1h tier is being
                # re-minted anyway. Cold-start dual writes downgrade to
                # severity=info / logger.info (not deleted — the GM panel
                # still sees them, per the OTEL Observability Principle) and
                # the warm pathology stays loud (severity=warn, lie-detector,
                # not hard error — the call already succeeded; the observation
                # is the waste).
                if cache_write_5m > 0 and cache_write_1h > 0:
                    both_writes_fields: dict[str, Any] = {
                        "iteration": iteration,
                        "cache_write_5m_tokens": cache_write_5m,
                        "cache_write_1h_tokens": cache_write_1h,
                        "cache_read_tokens": cache_read,
                        "model": response.model,
                    }
                    if cache_read > 0:
                        logger.warning(
                            "narrator.cache.both_writes_fired iter=%d 5m=%d 1h=%d "
                            "cache_read=%d model=%s",
                            iteration,
                            cache_write_5m,
                            cache_write_1h,
                            cache_read,
                            response.model,
                        )
                        _watcher_publish_event(
                            "narrator.cache.both_writes_fired",
                            both_writes_fields,
                            component="narrator.sdk",
                            severity="warn",
                        )
                    else:
                        logger.info(
                            "narrator.cache.both_writes_fired (cold-start mint, "
                            "expected) iter=%d 5m=%d 1h=%d model=%s",
                            iteration,
                            cache_write_5m,
                            cache_write_1h,
                            response.model,
                        )
                        _watcher_publish_event(
                            "narrator.cache.both_writes_fired",
                            both_writes_fields,
                            component="narrator.sdk",
                            severity="info",
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
                    caller=caller,
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
            # Story 71-23: deltas are emitted live during ``messages.stream``
            # above when ``on_text_delta`` is wired — do NOT re-fire the whole
            # block here (that would double-emit the iteration's prose).
            last_text = text or last_text

            if response.stop_reason != "tool_use":
                # Story 71-40: per-turn tool-loop summary. Fires once per
                # CONVERGED turn (independent of session_id, unlike the cost
                # events below), recording how many SDK round-trips the turn
                # consumed so the GM panel can spot runaway loops inflating
                # solo-turn p95.
                #
                # Story 82-9: ``caller`` tags the span so the non-narrator
                # dungeon-curate caller (materializer.py) is filtered OUT of the
                # narrator solo-turn p95 source. The converged path carries no
                # loop_exceeded marker (that is reserved for the raise path).
                with narrator_tool_loop_span(
                    iterations_used=iteration,
                    max_iterations=max_iterations,
                    caller=caller,
                ):
                    pass

                # Story 61-followup-D §C.3 — per-turn pulse for the GM
                # panel live counter. Fires once per successful turn, not
                # per tool-loop iteration; bypassed when session_id is
                # None (non-narrator paths).
                if session_id is not None:
                    self._emit_cost_running_total(
                        session_id=session_id,
                        model=last_model,
                    )
                    # Story 61-19 AC5 — per-turn cache-write split so the GM
                    # panel can spot churn regressions. Under the 61-19 tier
                    # layout the TTL tier IS the stable/tail distinction: the
                    # stable system prefix is the only 1h-marked content, and
                    # the volatile per-turn tail is the only 5m-marked content.
                    # A healthy session writes the stable prefix once (warmup)
                    # then reads it (1h write ~0 thereafter); the tail write
                    # recurs per turn but small. If a future field re-promotes
                    # growth into the volatile block, tail_write_tokens climbs
                    # and this event surfaces it. Fires once per turn (not per
                    # tool-loop iter), aggregating the loop's writes.
                    self._emit_cache_write_split(
                        stable_prefix_write_tokens=cumulative_cache_write_1h,
                        tail_write_tokens=cumulative_cache_write_5m,
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

        # Story 82-9: a turn that exhausts max_iterations and raises is the
        # WORST-latency turn — exactly the one the AC5 diagnosis most wants
        # iterations_used for — yet 71-40 emitted no summary span here, leaving
        # it invisible to the metric. Emit the per-turn summary before raising,
        # marked loop_exceeded=True so the GM panel can tell a ceiling-blown turn
        # from a deep-but-converged one. The fail-loud ceiling is unchanged.
        with narrator_tool_loop_span(
            iterations_used=max_iterations,
            max_iterations=max_iterations,
            caller=caller,
            loop_exceeded=True,
        ):
            pass

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
        # Story 91-4: the Haiku adapters keep their own rolling baselines
        # in the process-level ledger (keyed (session_id, caller)); the
        # same close_store eviction drops those too. Ledger cumulative/
        # announce state is intentionally NOT cleared — same scope as the
        # instance windows above (ADR-134 flagged follow-up).
        cost_safety.ledger().reset_baselines(session_id)

    def _maybe_emit_cost_runaway(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        model: str,
        session_id: str | None,
        caller: str = "narrator",
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

        # Story 91-4 — the warmup/clamp/trigger/priority comparator and
        # the event emit moved to ``cost_safety.check_and_emit_runaway``
        # so the Haiku adapters fire the SAME detector. This method keeps
        # the narrator's window storage (per-instance, keyed on plain
        # session_id — the 61-followup-A contract) and the append-after-
        # check ordering; only the evaluation is shared.
        cost_safety.check_and_emit_runaway(
            cost_window=self._cost_baseline.get(session_id),
            input_window=self._input_tokens_baseline.get(session_id),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            model=model,
            session_id=session_id,
            caller=caller,
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
        canonical message + actionable fields. Centralized (now in
        ``cost_safety.build_ceiling_exceeded``, 91-4) so no raise site —
        narrator or adapter — can drift in wording or field shape.
        """
        return cost_safety.build_ceiling_exceeded(
            session_id=session_id,
            cumulative=cumulative,
            ceiling_usd=self.session_cost_ceiling_usd,
        )

    def _check_cost_ceiling(self, session_id: str) -> None:
        """Pre-flight check at the entry of ``complete_with_tools``.

        Raises ``AnthropicSdkCostCeilingExceeded`` if the session's
        cumulative has already crossed the ceiling on a prior call —
        on ANY call site (story 91-4: the ledger pot is shared with the
        Haiku adapters). Terminal: the announce-set entry from the first
        crossing keeps subsequent refusals silent on the watcher (single
        emit per session), but the exception still re-raises so callers
        cannot accidentally swallow the kill.
        """
        cost_safety.ledger().check_ceiling(session_id, ceiling_usd=self.session_cost_ceiling_usd)

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
        Implementation lives in ``cost_safety.SessionCostLedger`` (91-4)
        so adapter spend feeds the same pot and the announce-once dedup
        spans call sites.
        """
        cost_safety.ledger().update_cumulative(
            session_id=session_id,
            cost_usd=cost_usd,
            model=model,
            ceiling_usd=self.session_cost_ceiling_usd,
        )

    def _emit_cache_write_split(
        self,
        *,
        stable_prefix_write_tokens: int,
        tail_write_tokens: int,
        model: str,
    ) -> None:
        """Per-turn cache-write split (Story 61-19 AC5).

        Splits the turn's cache_write into the amortizing stable prefix
        (1h-tier write) vs the volatile per-turn tail (5m-tier write) so the
        GM panel can plot write-churn and catch a regression that re-promotes
        a growing field into the volatile block. Fires once per successful
        turn (not per tool-loop iteration). Severity ``info`` — a routine
        baseline pulse, grouped under ``narrator.sdk`` with the sibling cache
        events.
        """
        _watcher_publish_event(
            "narrator.cache.write_split",
            {
                "stable_prefix_write_tokens": stable_prefix_write_tokens,
                "tail_write_tokens": tail_write_tokens,
                "total_write_tokens": stable_prefix_write_tokens + tail_write_tokens,
                "model": model,
            },
            component="narrator.sdk",
            severity="info",
        )

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

        Story 60-7 (supersedes 60-4): every iter — iter=1 included — marks the
        LAST content block of the newest user message. Story 61-19 (2026-05-30)
        sets that marker's TTL to ``_VOLATILE_CACHE_TTL`` (5m), NOT
        ``self.cache_ttl`` — the message tail is volatile, so it rides the 5m
        tier while the stable system prefix + tools keep ``self.cache_ttl``
        (1h). The marker's PRESENCE (every iter) is the 60-7 fix; its 5m VALUE
        is the 61-19 fix.

        Why marker every iter, not only on continuation: Anthropic auto-caches
        content that sits past the last explicit breakpoint at the default 5m
        TTL. The system_blocks[0] + tools[-1] prefix is marked at the
        configured TTL (1h by default), but the user message + recency-zone
        deltas added on iter=1 carry no marker by default, so the API
        auto-caches that tail at 5m. Story 60-7 added an EXPLICIT marker on the
        newest message every iter to pin that tail to a single write (the
        unmarked auto-5m would otherwise be displaced by the iter=2 marker —
        pure waste). Story 61-19 sets that marker's TTL to ``_VOLATILE_CACHE_TTL``
        (5m), NOT the configured 1h: the tail is volatile, so the iter=1 write
        lands at 5m deliberately and the within-turn iter=2 continuation reads
        it at 5m (seconds later) without re-minting. The 1h amortization lives
        on the stable prefix + tools (system_blocks[0] + tools[-1]), not on the
        message tail. (The 60-7 "$0.137 → $0.096" figure was the pre-61-19
        1h-tail layout; see ``sprint/archive/60-7-session.md`` and
        ``sprint/context/context-story-61-19.md``.)

        ``is_continuation`` is retained as caller-facing intent (iter=1 vs
        iter=2+) — useful to the call site and to test naming — but no
        longer branches the implementation. Both paths apply the marker.

        Bare-string content on the newest user message is promoted to a
        single-text-block list so ``cache_control`` (a content-block
        attribute) has somewhere to attach. The wire shape stays valid:
        Anthropic accepts both bare strings and block-list user content.

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
        """
        del is_continuation  # informational only; behavior is uniform across iters
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

        if not out:
            return out

        last_msg = out[-1]
        last_content = last_msg.get("content")
        if isinstance(last_content, str):
            # Promote bare string → single text block so cache_control has a
            # content-block to land on.
            promoted: list[dict[str, Any]] = [{"type": "text", "text": last_content}]
            last_msg["content"] = promoted
            last_content = promoted
        if isinstance(last_content, list) and last_content:
            last_block = last_content[-1]
            if isinstance(last_block, dict):
                # Story 61-19 — the newest message tail is VOLATILE (it changes
                # every turn). It still carries a marker (preserving 60-7's
                # single-write / within-turn-reuse property — the API would
                # otherwise auto-cache the post-prefix tail at 5m and the
                # continuation could displace it), but at the 5m volatile tier,
                # NOT ``self.cache_ttl``. Marking it 1h paid the 2x write
                # premium on content invalidated next turn — ~9.7k tok/turn of
                # waste (session 894). The stable system prefix keeps 1h
                # (``_build_system_array``); only this per-turn tail moves.
                last_block["cache_control"] = {
                    "type": "ephemeral",
                    "ttl": _VOLATILE_CACHE_TTL,
                }
            else:
                # No Silent Fallbacks: every live call site appends dict blocks
                # to running_messages, so a non-dict last block means an
                # upstream invariant has broken. Skipping the marker silently
                # would re-introduce the iter=1 auto-5m write we are paying
                # this whole story to eliminate — surface it loudly instead.
                logger.warning(
                    "_build_messages_payload: non-dict last block type=%s — "
                    "cache_control marker skipped (upstream invariant broken)",
                    type(last_block).__name__,
                )

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
        # complete_with_tools adds a moving cache_control breakpoint on the
        # newest tool_result message. Its PRESENCE stops the continuation from
        # re-minting this 1h tools+prefix cache (the 60-3 waste). Story 61-19
        # (2026-05-30): that message-level breakpoint is now 5m
        # (``_VOLATILE_CACHE_TTL``), not 1h — so the volatile tail rides 5m
        # while THIS tools array and system_blocks[0] keep ``self.cache_ttl``
        # (1h) and continue to read back at 1h on warm continuations
        # (probe-confirmed: warm-turn 1h write = 0).
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
