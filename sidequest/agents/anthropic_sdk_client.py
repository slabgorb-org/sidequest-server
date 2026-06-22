"""AnthropicSdkClient — narrator transport on ``claude-agent-sdk`` (ADR-101
amendment, Story 119-3).

The narrator inference path runs through the ``claude-agent-sdk`` ``query()``
loop over the Max **subscription** pool (the bundled CLI's OAuth login), NOT the
raw ``anthropic`` Messages SDK over the metered PAYG ledger. ``ANTHROPIC_API_KEY``
/ ``ANTHROPIC_AUTH_TOKEN`` must be **unset** (a set key re-routes to PAYG — the
119-1 NO-GO); :func:`assert_subscription_auth` enforces that loudly at call time
(No Silent Fallbacks). Context isolation (AC1) is pinned in
:func:`build_agent_sdk_options`.
"""

from __future__ import annotations

import atexit
import inspect
import logging
import os
import shutil
import tempfile
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    create_sdk_mcp_server,
    query,
)
from claude_agent_sdk import (
    tool as _sdk_tool,
)

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
    narrator_multi_text_block_discarded_span,
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


# Story 119-4 — honest OTEL labels for the subscription transport.
# ``auth_path='subscription'`` affirms a successful inference drew the free Max
# subscription pool (AC2 — the GM/cost-panel lie detector). ``cost_basis=
# 'notional'`` marks every cost figure as token×PAYG-rate NOTIONAL, not real
# billed dollars (AC4 — the bill is $0 under subscription auth, so the $10
# ceiling is a notional-shape signal). The auth-failure event makes an
# expired/absent login VISIBLE to the panel before the loud raise (AC1').
_AUTH_PATH_SUBSCRIPTION = "subscription"
_COST_BASIS_NOTIONAL = "notional"
_AUTH_UNAVAILABLE_EVENT = "narrator.auth_unavailable"


class AnthropicSdkClientError(LlmClientError):
    """Base error from AnthropicSdkClient."""


class AnthropicSdkConfigError(AnthropicSdkClientError):
    """Construction-time configuration problem (missing key, bad TTL)."""


class AgentSdkAuthUnavailable(AnthropicSdkClientError):
    """Story 119-3 (AC2): the claude-agent-sdk subscription path is not usable.

    Raised when a PAYG credential (``ANTHROPIC_API_KEY`` /
    ``ANTHROPIC_AUTH_TOKEN``) is SET on the SDK path — a set key silently
    re-routes to the metered API-platform ledger (the 119-1 NO-GO) — OR when the
    transport query fails: either a terminal ``is_error`` ResultMessage, or a
    *raised* query at the transport boundary (an absent/expired login is how
    OQ-5 surfaces, though a genuine transport fault is indistinguishable there —
    so the message names both causes and the chained cause carries the truth).
    A non-auth error from our OWN message processing is NOT mapped here; it
    propagates raw (Story 119-4 Reviewer round 2). There is NO PAYG fallback on
    this transport: the failure surfaces as a loud raise, never a
    degraded-but-successful result (No Silent Fallbacks).
    """


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


# Story 119-3 — the in-process SDK-MCP server name the narration tools are
# collected under. The model addresses each tool by ``mcp__<server>__<tool>``;
# the ``@tool`` handler bridge maps it back to the bare name for the registry
# (spec §5.2/§5.3).
_NARRATION_SERVER_NAME = "narration"


# Story 119-3 — process-stable neutral working dir for the agent SDK. The SDK
# treats ``cwd`` as the project root and would absorb the repo ``CLAUDE.md`` /
# ``.claude`` from the launch dir (the spike answered in an SM persona — AC1).
# An empty temp dir holds none of that.
_AGENT_SDK_CWD: str | None = None


def _cleanup_agent_sdk_cwd() -> None:
    """Remove the process-stable neutral cwd at interpreter exit (story 119-5).

    Registered with ``atexit`` the first (and only) time ``_neutral_cwd()``
    creates the dir, so the lazily-mkdtemp'd ``sidequest-agentsdk-cwd-*`` dir is
    not leaked one-per-process. Idempotent and best-effort: a missing dir is a
    no-op (``ignore_errors``), and the global is reset so a later
    ``_neutral_cwd()`` in the same process recreates and re-registers cleanly.
    """
    global _AGENT_SDK_CWD
    if _AGENT_SDK_CWD is not None:
        shutil.rmtree(_AGENT_SDK_CWD, ignore_errors=True)
        _AGENT_SDK_CWD = None


def _neutral_cwd() -> str:
    """Return a process-stable empty dir with no ``CLAUDE.md`` / ``.claude``."""
    global _AGENT_SDK_CWD
    if _AGENT_SDK_CWD is None:
        _AGENT_SDK_CWD = tempfile.mkdtemp(prefix="sidequest-agentsdk-cwd-")
        # Story 119-5: clean the temp dir up at process exit (no per-process
        # leak). Registered exactly once, when the dir is first created.
        atexit.register(_cleanup_agent_sdk_cwd)
    return _AGENT_SDK_CWD


def assert_subscription_auth() -> None:
    """Raise :class:`AgentSdkAuthUnavailable` if a PAYG credential is set
    (Story 119-3 AC2 — the INVERSE of the old "key required" check).

    The claude-agent-sdk transport draws the Max subscription pool only with
    ``ANTHROPIC_API_KEY`` AND ``ANTHROPIC_AUTH_TOKEN`` both unset; a set key
    silently re-routes to the metered PAYG ledger (the 119-1 NO-GO). There is
    no PAYG fallback — fail loud (No Silent Fallbacks).
    """
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            raise AgentSdkAuthUnavailable(
                f"{var} is set — the claude-agent-sdk transport must run with "
                "ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN both UNSET so auth "
                "resolves to the Max subscription login. A set credential "
                "re-routes to the metered PAYG ledger (the 119-1 NO-GO). Unset "
                "it; there is no PAYG fallback (No Silent Fallbacks)."
            )


def build_agent_sdk_options(
    *,
    model: str,
    system_prompt: str,
    max_turns: int,
    allowed_tools: list[str] | None = None,
    mcp_servers: dict[str, Any] | None = None,
    output_format: dict[str, Any] | None = None,
    thinking: dict[str, Any] | None = None,
) -> ClaudeAgentOptions:
    """Build the frozen ``ClaudeAgentOptions`` for one subscription call.

    Asserts the no-PAYG-cred invariant (AC2) and pins context isolation (AC1):
    a **plain-string** ``system_prompt`` (never the ``claude_code`` preset), a
    neutral ``cwd`` with no ``CLAUDE.md``, and ``setting_sources=[]`` +
    ``add_dirs=[]`` so no on-disk config tier (user/project/local) or repo
    ``CLAUDE.md`` is absorbed.

    ``max_turns`` is floored at 2 (the ``max(2, int(max_turns))`` guard below):
    the SDK spends an internal finalize turn, so a literal ``max_turns=1`` fails
    closed with ``subtype='error_max_turns'`` (the +1 is MANDATORY — spec §3.6 /
    OQ-16). 2 is the FLOOR, not a ceiling — callers may pass a HIGHER value for
    headroom and it only gives the ``tool-call → tool-result → finalize`` sequence
    more room to complete. The structured-output choke point (:func:`_call_haiku_sdk`)
    passes ``max_turns=4`` because a 2026-06-19 playtest showed the prompt-heavy
    router pass intermittently tripping ``error_max_turns`` at mt=2 (see the
    finalize-turn note below).

    Extended thinking is **disabled by default for any ``output_format`` call**
    (the Path-A structured-extraction sites: intent router, unseeded-objective
    classifier, archetype inference). The agent SDK implements ``output_format``
    as a synthetic ``StructuredOutput`` tool round-trip, which costs the
    tool-call → tool-result → finalize sequence (two turns at the mt=2 floor).
    The ``claude`` CLI defaults thinking ON ("adaptive"), so the model spends a
    ~1k-token thinking pass *before* the tool call; when that pass runs long the
    finalize cannot land inside 2 turns and the whole call fails
    ``error_max_turns`` — intermittently, scaling with how much the prompt gives
    it to think about (this is what took the intent-router spine dark on the
    119-3 subscription port: a structured classifier cannot afford a thinking
    turn at the mandatory mt=2 floor). Two mitigations now stack: disabling
    thinking (here) keeps these calls deterministic, ~3x faster, and ~6x cheaper
    in output tokens — a mechanical classifier reasons through its (heavily
    prescriptive) prompt, not a scratchpad — AND :func:`_call_haiku_sdk` raises
    the value passed to ``max_turns`` from the 2-floor to 4 for comfortable
    headroom, because a 2026-06-19 playtest showed the router pass *still*
    intermittently tripping ``error_max_turns`` at mt=2 on prompt-heavy passes. Callers that need thinking with structured output can still
    pass ``thinking`` explicitly to override. This builder does not *auto*-set
    ``thinking`` for non-``output_format`` callers — it stays ``None`` unless the
    caller passes it. Story 126-9: the narrator tool-loop and narrator-aside
    (``complete_with_tools``) now pass ``thinking={"type":"disabled"}`` explicitly
    to restore the pre-119-3 thinking-off baseline (the agent-SDK CLI defaults
    thinking ON/"adaptive", which was tripling narrator latency), so those callers
    are no longer ``None`` — the explicit kwarg is load-bearing, not redundant.
    """
    assert_subscription_auth()
    if output_format is not None and thinking is None:
        thinking = {"type": "disabled"}
    return ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        max_turns=max(2, int(max_turns)),
        allowed_tools=list(allowed_tools) if allowed_tools else [],
        mcp_servers=dict(mcp_servers) if mcp_servers else {},
        output_format=output_format,
        thinking=thinking,
        setting_sources=[],
        add_dirs=[],
        cwd=_neutral_cwd(),
    )


def _usage_int(usage: Any, key: str) -> int:
    """Read a token count from ``ResultMessage.usage`` (``dict | None`` — spec
    §3.2) or a usage object, defaulting to 0 when absent."""
    if usage is None:
        return 0
    if isinstance(usage, dict):
        return int(usage.get(key, 0) or 0)
    return int(getattr(usage, key, 0) or 0)


def _is_agent_result_message(msg: Any) -> bool:
    """Duck-typed terminal ``ResultMessage`` detector (fakes file §): the
    terminal message carries ``is_error`` + ``num_turns``; the streaming
    ``AssistantMessage`` carries ``content`` and neither."""
    return hasattr(msg, "is_error") and hasattr(msg, "num_turns")


def _build_narration_tool_handler(
    *,
    bare_name: str,
    tool_dispatch: Callable[[ToolUseBlock], Awaitable[ToolResultBlock] | ToolResultBlock],
    accumulator: list[ToolUseBlock],
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """The SDK-MCP → ``default_registry.dispatch`` bridge for one tool (spec §5.3).

    The agent SDK owns the loop and invokes this handler when the model calls
    the tool. The handler re-enters the orchestrator's ``dispatch`` closure with
    a ``ToolUseBlock`` carrying the **bare** tool name (the registry only knows
    bare names — ``Registry.dispatch`` looks up ``block.name``), appends it to
    the per-turn ledger (the fabricated-roll detector + GM-panel ledger depend
    on a complete ``tool_calls`` list), and returns the dispatch result in the
    SDK's ``{"content":[...],"is_error":...}`` shape so the SDK feeds it back to
    the model.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        block = ToolUseBlock(id=f"toolu_{uuid.uuid4().hex}", name=bare_name, arguments=args)
        accumulator.append(block)
        maybe = tool_dispatch(block)
        result = await maybe if inspect.isawaitable(maybe) else maybe
        return {
            "content": [{"type": "text", "text": result.content}],
            "is_error": result.is_error,
        }

    return handler


class AnthropicSdkClient:
    """Anthropic SDK client implementing ToolingLlmClient."""

    def __init__(self) -> None:
        # Story 119-3: the narrator transport is claude-agent-sdk over the Max
        # subscription pool. No ``ANTHROPIC_API_KEY`` is read or required —
        # construction never touches the network. The auth invariant is the
        # INVERSE of the old "key required" check (a SET key re-routes to PAYG —
        # the 119-1 NO-GO) and is asserted loudly at call time in
        # :func:`build_agent_sdk_options` (AC2, No Silent Fallbacks). The
        # late-bound module-level ``query`` symbol is the fake-injection seam
        # (OQ-9), monkeypatched by the test fleet — no ``sdk=`` injection.

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
        session_id: str | None = None,
        caller: str = "narrator",
        tool_choice: dict[str, Any] | None = None,
    ) -> ToolingResult:
        """Drive one narration turn through the claude-agent-sdk ``query()`` loop.

        Story 119-3: the manual ``messages.create`` + ``stop_reason=='tool_use'``
        re-call loop is replaced by the SDK's own loop. The system blocks are
        concatenated into a plain-string ``system_prompt`` (AC1 isolation); the
        narration tools become in-process SDK-MCP ``@tool`` handlers that bridge
        back into ``tool_dispatch`` (spec §5.3); the SDK runs the loop and emits a
        terminal ``ResultMessage`` carrying the converged prose, ``num_turns``,
        and ``usage``. The ``ToolingResult`` shape and every OTEL signal are
        preserved (spec §4/§7.4).

        ``max_tokens`` and ``tool_choice`` are accepted for ToolingLlmClient
        signature compatibility but not forwarded: the CLI owns output length and
        the Agent SDK exposes no ``tool_choice`` (spec §3.6). A read-only caller
        (the aside, ``tool_choice={"type":"none"}``) passes no ``tool_dispatch``,
        so no tools are advertised — the agent SDK's analog of "present no tools".
        """
        del max_tokens, tool_choice  # signature-compat only (see docstring)

        # Story 61-followup-D §C.2 — pre-flight ceiling check. A session whose
        # cumulative already crossed the ceiling MUST raise before any spend.
        if session_id is not None:
            self._check_cost_ceiling(session_id)

        # AC1: the assembled narrator system text is a PLAIN STRING (never the
        # claude_code preset). The three-zone cacheable layout collapses to one
        # string — the CLI owns caching now, so the per-block cache markers are
        # gone (spec §6.4.3 / OQ-6).
        system_prompt = "\n\n".join(b.text for b in system_blocks if b.text)
        # The SDK owns the tool round-trip, so only the initial user turn(s) are
        # sent as the prompt; tool_result continuations are no longer hand-built.
        prompt = "\n\n".join(
            m.content for m in messages if isinstance(m.content, str) and m.content
        )

        all_tool_uses: list[ToolUseBlock] = []
        mcp_servers, allowed_tools = self._build_narration_mcp(tools, tool_dispatch, all_tool_uses)
        options = build_agent_sdk_options(
            model=model,
            system_prompt=system_prompt,
            max_turns=max_iterations,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            # Story 126-9: the narrator tool-loop runs with extended thinking
            # DISABLED. The 119-3 agent-sdk port (f970091e) moved this call from
            # the ``anthropic`` Messages SDK (thinking OFF unless a budget is
            # passed) onto the ``claude-agent-sdk`` ``query()`` loop, whose CLI
            # defaults thinking ON ("adaptive"). Left unset, sonnet-4.6 then ran
            # an adaptive thinking pass before EACH of up to ``max_iterations``
            # tool-loop iterations, tripling agent_duration_ms (~16s -> ~50-57s;
            # same-world proof wry_whimsy/oz 15.9 -> 56.7). Passing it explicitly
            # here (the builder only auto-disables for ``output_format`` calls,
            # lines 262-263) restores the pre-119-3 baseline — a behaviour
            # restore, NOT a quality cut. One call site, so this also covers the
            # narrator-aside (aside_resolver.py: ``caller="aside"``). If thinking
            # is ever wanted it must be a deliberate, bounded opt-in, never an
            # adaptive default firing once per iteration.
            thinking={"type": "disabled"},
        )

        last_text = ""
        last_model = model
        result_msg: Any = None
        with llm_request_span(model=model) as span:
            # Story 119-4 (Reviewer round 2 — the [HIGH] mislabel fix): drive the
            # async iterator by hand so the auth-mapping catch wraps ONLY the
            # transport boundary (``anext`` — the SDK producing the next message).
            # An absent/expired subscription login surfaces HERE as a raised query
            # (OQ-5), so a transport-boundary failure maps to the typed auth error
            # and fires the GM-panel event. Our OWN message processing runs OUTSIDE
            # that catch: a parse bug there propagates RAW, never relabeled as an
            # auth failure on the lie-detector panel (the panel exists to END
            # winging it — it must not assert a diagnosis it cannot support).
            message_stream = aiter(query(prompt=prompt, options=options))
            while True:
                try:
                    message = await anext(message_stream)
                except StopAsyncIteration:
                    break
                except AgentSdkAuthUnavailable:
                    # Already the typed, GM-panel-visible auth error — as-is.
                    raise
                except Exception as exc:
                    # max_turns exhaustion can surface HERE as a *raised* query
                    # (the SDK throwing "Reached maximum number of turns (N)")
                    # rather than as a terminal is_error ResultMessage. That is a
                    # tool-loop non-convergence, NOT an auth/transport fault — so
                    # branch on the SDK's documented signal and raise the accurate
                    # type, mirroring the terminal ``error_max_turns`` branch
                    # below. Mapping it to AgentSdkAuthUnavailable actively
                    # misdirects debugging (sq-playtest 2026-06-22: a combat-starve
                    # max_turns error read as "subscription login absent/expired"
                    # and sent the operator chasing auth before the embedded
                    # "Reached maximum number of turns" string revealed the real
                    # cause). This is NOT auth — emit NO auth-unavailable event.
                    if "maximum number of turns" in str(exc).lower():
                        with narrator_tool_loop_span(
                            iterations_used=max(2, max_iterations),
                            max_iterations=max_iterations,
                            caller=caller,
                            loop_exceeded=True,
                        ):
                            pass
                        raise AnthropicSdkLoopExceeded(
                            "agent-sdk tool loop did not converge — max_turns "
                            "exhaustion raised at the transport boundary "
                            f"({type(exc).__name__}: {exc}; "
                            f"max_turns={max(2, max_iterations)})"
                        ) from exc
                    # The transport boundary failed: an absent/expired subscription
                    # login OR a genuine transport fault — we cannot disambiguate
                    # at the boundary. Map to the typed auth error and emit the
                    # GM-panel event BEFORE the loud raise, but the headline does
                    # NOT assert auth as the SOLE cause (the chained cause/detail
                    # carries the truth), so a transport fault is never reported as
                    # a confirmed login expiry (No Silent Fallbacks).
                    self._emit_auth_unavailable(
                        reason="query_raised", model=model, caller=caller, detail=str(exc)
                    )
                    raise AgentSdkAuthUnavailable(
                        "claude-agent-sdk query failed "
                        f"({type(exc).__name__}: {exc}) — subscription login "
                        "absent/expired or a transport error; no PAYG fallback "
                        "(No Silent Fallbacks)."
                    ) from exc
                # --- message processing (OURS, not the transport's): exceptions
                # here propagate raw and are NEVER coerced into the auth signal ---
                if _is_agent_result_message(message):
                    result_msg = message
                    continue
                model_id = getattr(message, "model", None)
                if model_id:
                    last_model = model_id
                content = getattr(message, "content", None)
                if isinstance(content, list):
                    text_chunks = [b.text for b in content if getattr(b, "type", None) == "text"]
                    # Playtest 2026-06-07 (five_points doubled-narration): keep
                    # only the LAST text block of an assistant message; earlier
                    # blocks are drafts. Emit a WARNING span so the drop is
                    # audited, never silent (house OTEL rule, spec §7.3).
                    if len(text_chunks) > 1:
                        discarded_chars = sum(len(c) for c in text_chunks[:-1])
                        logger.warning(
                            "narrator.multi_text_block_discarded count=%d "
                            "discarded_chars=%d kept_chars=%d caller=%s",
                            len(text_chunks) - 1,
                            discarded_chars,
                            len(text_chunks[-1]),
                            caller,
                        )
                        with narrator_multi_text_block_discarded_span(
                            discarded_count=len(text_chunks) - 1,
                            discarded_chars=discarded_chars,
                            kept_chars=len(text_chunks[-1]),
                            iteration=1,
                            caller=caller,
                        ):
                            pass
                    if text_chunks:
                        last_text = text_chunks[-1]

            if result_msg is None:
                raise AnthropicSdkClientError(
                    "claude-agent-sdk query produced no terminal ResultMessage "
                    f"(caller={caller!r}, model={model!r}) — cannot account for "
                    "the call (No Silent Fallbacks)."
                )

            # Usage / cost accounting from ResultMessage.usage (dict|None, §3.2).
            usage = getattr(result_msg, "usage", None)
            input_tokens = _usage_int(usage, "input_tokens")
            output_tokens = _usage_int(usage, "output_tokens")
            cache_read = _usage_int(usage, "cache_read_input_tokens")
            cache_write = _usage_int(usage, "cache_creation_input_tokens")
            cost = compute_cost_usd(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_read_tokens=cache_read,
                cached_input_write_tokens=cache_write,
                model=last_model,
            )
            # Story 119-4 (AC3'): the agent SDK's own per-call spend view. The
            # claude-agent-sdk subprocess does NOT expose the anthropic-ratelimit-*
            # headers (the raw-SDK signal is unreachable here), so ResultMessage.
            # total_cost_usd is the only transport-reported figure — surface it
            # distinct from our notional cost so a non-zero value flags a PAYG leak.
            sdk_reported_cost_usd = getattr(result_msg, "total_cost_usd", None)
            span.set_attributes(
                {
                    "llm.caller": caller,
                    "llm.input_tokens": input_tokens,
                    "llm.output_tokens": output_tokens,
                    "llm.cached_input_read_tokens": cache_read,
                    "llm.cached_input_write_tokens": cache_write,
                    "llm.cost_usd": cost,
                    # Story 119-4 (AC2): affirm this inference drew the free
                    # subscription pool — the GM/cost-panel lie detector.
                    "llm.auth_path": _AUTH_PATH_SUBSCRIPTION,
                }
            )
            subtype = getattr(result_msg, "subtype", None)
            if subtype:
                span.set_attribute("llm.stop_reason", str(subtype))
            logger.info(
                "narrator.sdk.usage caller=%s model=%s input=%d output=%d "
                "cache_read=%d cache_write=%d cost_usd=%.6f",
                caller,
                last_model,
                input_tokens,
                output_tokens,
                cache_read,
                cache_write,
                cost,
            )
            _watcher_publish_event(
                "narrator.sdk.usage",
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost,
                    "model": last_model,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                    # Story 119-4: AC2 affirmative free-pool tag, AC4 notional
                    # cost-basis (token×PAYG-rate, not a real bill), AC3' the
                    # transport's own reported spend (PAYG-leak tell).
                    "auth_path": _AUTH_PATH_SUBSCRIPTION,
                    "cost_basis": _COST_BASIS_NOTIONAL,
                    "sdk_reported_cost_usd": sdk_reported_cost_usd,
                },
                component="narrator.sdk",
                severity="info",
            )
            # Story 61-4 — cost-runaway fingerprint detector (session_id=None is
            # a no-op). Story 61-followup-D — per-call cumulative + $10 ceiling.
            self._maybe_emit_cost_runaway(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                model=last_model,
                session_id=session_id,
                caller=caller,
            )
            if session_id is not None:
                self._update_session_cumulative(
                    session_id=session_id, cost_usd=cost, model=last_model
                )

        num_turns = int(getattr(result_msg, "num_turns", 1) or 1)

        if getattr(result_msg, "is_error", False):
            if getattr(result_msg, "subtype", "") == "error_max_turns":
                # Story 82-9: the worst-latency turn still emits the summary span
                # (marked loop_exceeded) before the fail-loud raise.
                with narrator_tool_loop_span(
                    iterations_used=num_turns,
                    max_iterations=max_iterations,
                    caller=caller,
                    loop_exceeded=True,
                ):
                    pass
                raise AnthropicSdkLoopExceeded(
                    "agent-sdk tool loop did not converge "
                    f"(subtype=error_max_turns, num_turns={num_turns}, "
                    f"max_turns={max(2, max_iterations)})"
                )
            # An auth/credit/transport failure surfaces as is_error — raise, never
            # return a degraded-success result that masks the missing credential.
            # Story 119-4 (AC1'): announce it to the GM panel before the raise.
            self._emit_auth_unavailable(
                reason="is_error",
                model=last_model,
                caller=caller,
                detail=f"subtype={getattr(result_msg, 'subtype', None)!r}",
            )
            raise AgentSdkAuthUnavailable(
                "claude-agent-sdk query failed (is_error, "
                f"subtype={getattr(result_msg, 'subtype', None)!r}) — subscription "
                "login absent or query rejected; no PAYG fallback (No Silent "
                "Fallbacks)."
            )

        # Story 71-40 / 82-9: soft cap-hit + per-turn tool-loop summary span.
        if iteration_cap is not None and num_turns >= iteration_cap:
            with narrator_tool_loop_cap_hit_span(
                iteration_cap=iteration_cap,
                iterations_used=num_turns,
                max_iterations=max_iterations,
            ):
                pass
        with narrator_tool_loop_span(
            iterations_used=num_turns,
            max_iterations=max_iterations,
            caller=caller,
        ):
            pass
        if session_id is not None:
            self._emit_cost_running_total(session_id=session_id, model=last_model)

        text = getattr(result_msg, "result", None) or last_text
        return ToolingResult(
            text=text,
            stop_reason="end_turn",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_read_tokens=cache_read,
            cached_input_write_tokens=cache_write,
            model=last_model,
            tool_calls=all_tool_uses,
            cumulative_cost_usd=cost,
            # OQ-6: the agent SDK does not expose the per-TTL cache-creation
            # split (5m/1h). Documented-zero, NOT measured — the CLI owns
            # caching; never report a measured 0 (No Silent Fallbacks).
            cached_input_write_5m_tokens=0,
            cached_input_write_1h_tokens=0,
        )

    def _build_narration_mcp(
        self,
        tools: list[ToolDefinition],
        tool_dispatch: Callable[[ToolUseBlock], Awaitable[ToolResultBlock] | ToolResultBlock]
        | None,
        accumulator: list[ToolUseBlock],
    ) -> tuple[dict[str, Any], list[str]]:
        """Build the per-turn in-process SDK-MCP server + allowed_tools list from
        the ruleset-filtered tool catalog (spec §5).

        Each ``ToolDefinition`` becomes a ``@tool``-decorated handler (the §5.3
        dispatch bridge) collected under :data:`_NARRATION_SERVER_NAME`. The set
        is rebuilt per call because it is ruleset-filtered (a static server would
        advertise the wrong tools — No Silent Fallbacks). A toolless call (no
        tools, or no ``tool_dispatch`` — the read-only aside / fabricated-roll
        rewrite) advertises nothing.
        """
        if not tools or tool_dispatch is None:
            return {}, []
        sdk_tools = []
        allowed: list[str] = []
        for t in tools:
            handler = _build_narration_tool_handler(
                bare_name=t.name, tool_dispatch=tool_dispatch, accumulator=accumulator
            )
            sdk_tools.append(_sdk_tool(t.name, t.description, t.input_schema)(handler))
            allowed.append(f"mcp__{_NARRATION_SERVER_NAME}__{t.name}")
        server = create_sdk_mcp_server(name=_NARRATION_SERVER_NAME, tools=sdk_tools)
        return {_NARRATION_SERVER_NAME: server}, allowed

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

    def _emit_auth_unavailable(
        self,
        *,
        reason: str,
        model: str,
        caller: str,
        detail: str,
    ) -> None:
        """Story 119-4 (AC1'): announce a subscription-auth failure to the GM
        panel BEFORE the loud raise.

        Fires from the two transport auth-failure surfaces only: ``reason=
        "is_error"`` (a terminal ``is_error`` ResultMessage) and ``reason=
        "query_raised"`` (the transport boundary raised — an absent/expired login
        or a transport fault). It does NOT fire for a tool-loop ``error_max_turns``
        (a convergence failure, raised earlier) or for a non-auth error in our own
        message processing (which propagates raw). An expired/absent OAuth login
        must be VISIBLE to the cost panel (the lie detector), not just a stack
        trace in the logs — so the operator can tell "the narrator fell over
        because the login expired" from any other server error. There is no PAYG
        fallback; this event always precedes a raise.
        """
        logger.error(
            "narrator.auth_unavailable reason=%s model=%s caller=%s detail=%s",
            reason,
            model,
            caller,
            detail,
        )
        _watcher_publish_event(
            _AUTH_UNAVAILABLE_EVENT,
            {
                "reason": reason,
                "model": model,
                "caller": caller,
                "detail": detail,
            },
            component="narrator.sdk",
            severity="error",
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
                # Story 119-4 (AC4): the "X / $10" denominator is NOTIONAL
                # (token×PAYG-rate) under subscription auth, not real dollars.
                "cost_basis": _COST_BASIS_NOTIONAL,
            },
            component="narrator.sdk",
            severity="info",
        )
