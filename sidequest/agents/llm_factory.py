"""LlmClient factory — selects backend from env (ADR-073 Phase 1/2).

Story 91-1 (epic 91 "Dark Spend"): this module is also the **single SDK
choke point** — :func:`build_async_anthropic` is the sole ``AsyncAnthropic``
construction site in the server, and :func:`_record_usage_telemetry` is the
uniform per-call usage accounting (log line + ``llm.request`` span attributes
+ ``cost_usd`` + caller tag) every Anthropic call flows through.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Literal

from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.anthropic_sdk_client import (
    _EXTENDED_CACHE_TTL_BETA,
    AnthropicSdkClient,
)
from sidequest.agents.claude_client import LlmClient, LlmClientError
from sidequest.agents.tooling_protocol import ToolingLlmClient
from sidequest.telemetry.spans.intent_router import intent_router_cache_floor_span
from sidequest.telemetry.spans.llm_request import llm_request_span

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

# Canonical Anthropic beta opt-in for ttl:"1h" ephemeral cache lives in
# ``anthropic_sdk_client`` (the narrator's 1h path). Import it rather than
# duplicate the wire string — a drifted copy would silently 400 every Haiku
# turn (No Silent Fallbacks).

ENV_BACKEND = "SIDEQUEST_LLM_BACKEND"
ENV_OLLAMA_URL = "SIDEQUEST_OLLAMA_URL"

_VALID_BACKENDS = frozenset({"claude", "ollama", "anthropic_sdk"})
_RETIRED_BACKENDS = frozenset({"claude", "ollama"})


class UnknownBackend(LlmClientError):
    """SIDEQUEST_LLM_BACKEND value was not one of the supported backends."""


class NarratorBackendRetired(LlmClientError):
    """Story 61-9 / ADR-101 amendment: ``claude`` and ``ollama`` are retired.

    The SDK (``anthropic_sdk``) is the sole viable backend for both narrator
    and tool callers — the retired backends do not implement the tool-use
    contract (``complete_with_tools``), so handing them to any caller is a
    deferred-failure trap. Fail at the config boundary instead (NO-FALLBACK
    per project memory ``feedback_no_fallbacks_hard``).
    """


def build_async_anthropic() -> AsyncAnthropic:
    """Construct the Anthropic SDK client — the SINGLE construction site.

    Story 91-1 (epic 91 "Dark Spend"): every ``AsyncAnthropic`` in the server
    is built here so usage instrumentation cannot be bypassed by an ad-hoc
    construction. Consumers must look this function up late-bound (through
    the module dict at call time, e.g. ``llm_factory.build_async_anthropic()``
    or a function-level ``from ... import``) so the wiring test's
    monkeypatched fake is what every adapter receives.

    Fails loudly when ``ANTHROPIC_API_KEY`` is unset — No Silent Fallbacks.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LlmClientError(
            "ANTHROPIC_API_KEY not set — required to construct the Anthropic "
            "SDK client (story 91-1 single choke point). No silent fallback."
        )
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=api_key)


def _record_usage_telemetry(
    span: Any,
    resp: Any,
    *,
    caller: str,
    request_model: str,
) -> None:
    """Uniform per-call usage accounting (story 91-1, OTEL Observability
    Principle applied to money): stamp token/cost/caller attributes onto the
    ``llm.request`` span AND emit the uniform ``llm.sdk.usage`` INFO line so
    both Jaeger and log-based accounting see every call.

    ``cached_input_read_tokens`` remains the lie-detector field — it goes
    non-zero on turn 2+ once a static prefix is warm, proving the cache
    actually engaged rather than Claude just claiming a cheap turn.

    A response without a ``usage`` block is a real condition to surface —
    a call we cannot account for is exactly the dark spend this epic
    eliminates — so it raises rather than logging a zero-cost line
    (No Silent Fallbacks).

    ``request_model`` is the model id the adapter put on the request; the
    response's own ``model`` field (the billed id) wins when present.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        raise LlmClientError(
            f"Anthropic response for caller={caller!r} carried no usage block "
            "— the call cannot be cost-accounted (epic 91 Dark Spend). "
            "Refusing to emit a zero-cost usage line (No Silent Fallbacks)."
        )
    model = str(getattr(resp, "model", "") or "") or request_model
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cost = compute_cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_read_tokens=cache_read,
        cached_input_write_tokens=cache_write,
        model=model,
    )
    span.set_attribute("llm.caller", caller)
    span.set_attribute("llm.input_tokens", input_tokens)
    span.set_attribute("llm.output_tokens", output_tokens)
    span.set_attribute("llm.cached_input_read_tokens", cache_read)
    span.set_attribute("llm.cached_input_write_tokens", cache_write)
    span.set_attribute("llm.cost_usd", cost)
    stop_reason = getattr(resp, "stop_reason", None)
    if stop_reason:
        span.set_attribute("llm.stop_reason", str(stop_reason))
    logger.info(
        "llm.sdk.usage caller=%s model=%s input=%d output=%d "
        "cache_read=%d cache_write=%d cost_usd=%.6f",
        caller,
        model,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
        cost,
    )


def build_llm_client(
    *, purpose: Literal["narrator", "tool"] = "narrator"
) -> LlmClient | ToolingLlmClient:
    """Return the configured LlmClient. Default: AnthropicSdkClient.

    ``purpose`` declares the caller's intent (narrator prompt-build vs.
    tool-only consumer such as the dungeon ``curate`` stage). The kwarg is
    keyword-only so future param additions cannot silently shift positional
    meaning.

    Post-story-61-9 / ADR-101 amendment: only ``anthropic_sdk`` is viable
    for any caller. Setting ``SIDEQUEST_LLM_BACKEND`` to ``claude`` or
    ``ollama`` raises :class:`NarratorBackendRetired` at construction
    regardless of ``purpose`` — those backends do not implement the
    tool-use contract, so failing here keeps the error at the config
    boundary instead of at first method call.

    Fails loudly for unknown backend values — no silent fallback (CLAUDE.md).
    """
    raw = os.environ.get(ENV_BACKEND, "anthropic_sdk")
    key = raw.strip().lower()
    if key not in _VALID_BACKENDS:
        raise UnknownBackend(
            f"{ENV_BACKEND}={raw!r} not supported; pick one of {sorted(_VALID_BACKENDS)}"
        )
    if key in _RETIRED_BACKENDS:
        raise NarratorBackendRetired(
            f"{ENV_BACKEND}={raw!r} is retired for purpose={purpose!r}. "
            "ADR-101 (amended by story 61-9): the Anthropic SDK "
            "(anthropic_sdk) is the sole viable narrator backend, and the "
            "retired backends (claude, ollama) do not implement the "
            "tool-use contract required by any caller. Set "
            f"{ENV_BACKEND}=anthropic_sdk (or unset for the default)."
        )
    if key == "anthropic_sdk":
        return AnthropicSdkClient()
    # Unreachable — the set check above covers all known backends.
    raise UnknownBackend(f"backend {key!r} recognised but not wired")


# ADR-101 per-call routing: an aside is the lowest-drama input in the
# system (SOUL.md "Cost Scales with Drama"), so it routes to the cheapest
# Haiku tier as a single-shot completion — NOT the narrator's tool-use
# loop. Distinct call site, not a reinvention of the narrator client.
_ASIDE_MODEL = "claude-haiku-4-5-20251001"


class _AsideLlm:
    """Single-shot Haiku adapter satisfying ``AsideResolver``'s ``AsideLLM``.

    Obtains its SDK through :func:`build_async_anthropic` — the single
    construction site (story 91-1). Fails loudly if ``ANTHROPIC_API_KEY``
    is unset — No Silent Fallbacks.
    """

    def __init__(self) -> None:
        self._sdk = build_async_anthropic()

    async def complete(self, *, system: str, user: str) -> str:
        # ``system`` stays a BARE string — NOT a cached content block. The
        # aside prompt is ~361 tokens, far below Haiku 4.5's 4,096-token
        # cacheable-prefix floor, so a ``cache_control`` marker here is
        # accepted by the API but silently never caches. Adding one would
        # imply caching that does not happen (No Silent Fallbacks). The
        # Intent Router (``_IntentRouterLlm``) is the every-turn cost driver
        # and clears the floor — that is where caching pays off.
        #
        # Story 91-1: the call runs inside an ``llm.request`` span with the
        # uniform usage accounting — pre-91-1 this path emitted NO telemetry
        # at all and was structurally invisible to cost forensics.
        with llm_request_span(model=_ASIDE_MODEL) as span:
            resp = await self._sdk.messages.create(
                model=_ASIDE_MODEL,
                system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=512,
            )
            _record_usage_telemetry(span, resp, caller="aside", request_model=_ASIDE_MODEL)
        return "".join(block.text for block in resp.content if block.type == "text")


def build_aside_llm() -> _AsideLlm:
    """Build the Haiku-tier LLM for out-of-band aside resolution (ADR-107)."""
    return _AsideLlm()


# ADR-113 Intent Router producer: pre-narrator classification call. Mirrors
# the ``_AsideLlm`` pattern verbatim — single-shot Haiku via SDK, fails loud
# on missing API key, no fallback adapter. Model id flows from the per-call
# routing ladder (``CallType.CLASSIFICATION``) so a future ladder revision
# moves the constant with it.
_INTENT_ROUTER_MODEL = "claude-haiku-4-5-20251001"

# The marker on the system block caches the whole tools+system prefix (canonical
# cache order tools → system → messages). For the Intent Router that combined
# prefix is ~4,730 tokens (DispatchPackage tool schema ~1,970 tok + system prompt
# ~2,760 tok), which clears Haiku 4.5's 4,096-token cacheable floor. NOTE: the
# system prompt ALONE is below the floor — the whole margin comes from bundling
# the tool schema, so the floor guard checks the COMBINED prefix (see
# test_haiku_cache_control.py), not the system block in isolation. 1h — not 5m —
# because the submit-and-wait MP turn cadence (a slow typist at the table) can
# space these Haiku calls minutes apart; a 5m prefix would expire between turns.
# Matches the stable-prefix TTL the narrator keeps in ``anthropic_sdk_client``.
_INTENT_ROUTER_CACHE_TTL = "1h"


# Story 91-3 (epic 91 "Dark Spend"): Haiku 4.5's minimum cacheable prompt
# length. Below this floor a ``cache_control`` marker is accepted by the API
# and *silently never caches* — the request succeeds, the bill re-charges the
# full prefix every turn, and nothing reports the no-op. The build-time guard
# below turns that silent trap into a loud build failure.
HAIKU_CACHEABLE_PREFIX_FLOOR_TOKENS = 4096

# Offline chars→tokens calibration for the floor guard. Measured against the
# live ``count_tokens`` endpoint on 2026-06-05: the combined tools+system
# prefix was 15,425 chars / 4,730 tokens = 3.261 chars/token. 3.2 keeps the
# guard's threshold (~13.1k chars) just BELOW the CI tripwire in
# ``test_haiku_cache_control.py`` (13,500 chars), so a shrinking prefix fails
# in CI before this runtime guard would start killing live turns. The
# authoritative re-measure after any prompt/schema change is the opt-in
# ``test_intent_router_prefix_token_floor_live`` (count_tokens — exact).
_PREFIX_CHARS_PER_TOKEN = 3.2


class IntentRouterCacheFloorError(LlmClientError):
    """The Intent Router's combined cacheable prefix is below Haiku's floor.

    Story 91-3: raised at client-build time by :func:`build_intent_router_llm`
    when the estimated tools+system prefix drops under
    :data:`HAIKU_CACHEABLE_PREFIX_FLOOR_TOKENS`. Never ship a ``cache_control``
    marker that silently doesn't cache (No Silent Fallbacks) — the epic-91
    incident was exactly this trap: 100% uncached Haiku at full price, every
    turn, invisible until cost forensics.
    """


def _estimate_intent_router_prefix_tokens() -> tuple[int, int]:
    """Estimate the combined tools+system cacheable prefix, offline.

    Returns ``(prefix_chars, estimated_tokens)``. The prefix is everything the
    ``cache_control`` marker on the system block covers (canonical cache order
    tools → system → messages): the system prompt PLUS the DispatchPackage
    tool name/description/schema. Measuring the system block alone was the
    original defect — it is sub-floor by itself; the margin comes entirely
    from bundling the schema.

    Reads the production prompt/schema LATE-BOUND through the
    ``sidequest.agents.intent_router`` module at call time (same monkeypatch
    doctrine as :func:`build_async_anthropic`). The import is function-level
    because ``intent_router`` imports this module at module scope.

    Offline by design: the adapter is rebuilt once per turn
    (``build_intent_router_for_session``), so a ``count_tokens`` network call
    here would add a per-turn round-trip for a value that only changes when
    code changes.
    """
    import sidequest.agents.intent_router as intent_router

    prefix_chars = (
        len(intent_router._SYSTEM_PROMPT)
        + len(json.dumps(intent_router._dispatch_tool_schema()))
        + len(intent_router._TOOL_NAME)
        + len(intent_router._TOOL_DESCRIPTION)
    )
    return prefix_chars, int(prefix_chars / _PREFIX_CHARS_PER_TOKEN)


class IntentRouterEmptyResponse(LlmClientError):
    """Haiku returned a response with no ``tool_use`` block.

    Distinct from a transport error or a schema-invalid tool input — the
    SDK call succeeded but the model emitted no usable tool call (refusal,
    pause-turn, max-tokens-before-first-byte, or an unexpected
    text-only content array despite a forced ``tool_choice``). Carries
    ``stop_reason``, content block types, and usage in the message so the
    failure mode is identifiable in logs and OTEL ``raw_preview`` instead
    of surfacing downstream as a confusing validation error on ``None``.
    """


class _IntentRouterLlm:
    """Single-shot Haiku adapter satisfying the Intent Router's ``IntentRouterLLM``.

    Same shape as :class:`_AsideLlm` — eagerly obtains its SDK through
    :func:`build_async_anthropic` (the single construction site, story 91-1)
    so the build-time environment check fires loudly (memory rule
    ``feedback_no_fallbacks_hard``).
    """

    def __init__(self) -> None:
        self._sdk = build_async_anthropic()

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Force a single tool call and return its structured input (ADR-102).

        ``tool_choice`` pins the model to ``tool_name`` so the response
        carries a ``tool_use`` block whose ``input`` is already structured
        — no free-text JSON to parse, no markdown fences to strip.

        The static ``system`` prompt is sent as a 1h ephemeral cache block.
        One marker on the system block caches the whole tools+system prefix
        (canonical cache order is tools → system → messages), so both the
        DispatchPackage schema and the ~2,760-token prompt read back on turn
        2+ instead of re-billing every player turn. ``ttl:"1h"`` is a beta —
        without the ``extended-cache-ttl`` header the API 400-rejects the
        request, so the header is mandatory, not optional (No Silent
        Fallbacks). The call runs inside an ``llm.request`` span so the GM
        panel can confirm the cache is live (OTEL Observability Principle).
        """
        with llm_request_span(model=_INTENT_ROUTER_MODEL) as span:
            resp = await self._sdk.messages.create(
                model=_INTENT_ROUTER_MODEL,
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {
                            "type": "ephemeral",
                            "ttl": _INTENT_ROUTER_CACHE_TTL,
                        },
                    }
                ],
                messages=[{"role": "user", "content": user}],
                tools=[
                    {
                        "name": tool_name,
                        "description": tool_description,
                        "input_schema": tool_schema,
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
                max_tokens=2048,
                extra_headers={"anthropic-beta": _EXTENDED_CACHE_TTL_BETA},
            )
            _record_usage_telemetry(
                span, resp, caller="intent_router", request_model=_INTENT_ROUTER_MODEL
            )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return dict(block.input)
        block_types = [getattr(b, "type", "?") for b in resp.content]
        usage_repr: str
        try:
            usage_repr = repr(resp.usage.model_dump())
        except Exception:  # noqa: BLE001 — usage shape varies by SDK version
            usage_repr = repr(getattr(resp, "usage", None))
        raise IntentRouterEmptyResponse(
            f"Haiku returned no tool_use block "
            f"(stop_reason={resp.stop_reason!r}, blocks={block_types}, "
            f"usage={usage_repr})"
        )


def build_intent_router_llm() -> _IntentRouterLlm:
    """Build the Haiku-tier LLM for the Intent Router producer (ADR-113).

    Story 91-3 fail-loud floor guard: validates the combined tools+system
    cacheable prefix against Haiku 4.5's
    :data:`HAIKU_CACHEABLE_PREFIX_FLOOR_TOKENS` BEFORE constructing the
    adapter, and raises :class:`IntentRouterCacheFloorError` if the estimate
    is sub-floor — below the floor the adapter's ``cache_control`` marker is
    accepted by the API and silently never caches (the epic-91 dark-spend
    incident). The decision is emitted as an ``intent_router.cache_floor``
    span on both paths so the GM panel can verify the guard engaged (OTEL
    Observability Principle).
    """
    prefix_chars, estimated_tokens = _estimate_intent_router_prefix_tokens()
    passed = estimated_tokens >= HAIKU_CACHEABLE_PREFIX_FLOOR_TOKENS
    with intent_router_cache_floor_span(
        passed=passed,
        floor_tokens=HAIKU_CACHEABLE_PREFIX_FLOOR_TOKENS,
        estimated_tokens=estimated_tokens,
        prefix_chars=prefix_chars,
    ):
        pass
    if not passed:
        logger.error(
            "intent_router.cache_floor REFUSED build: estimated_tokens=%d "
            "floor_tokens=%d prefix_chars=%d",
            estimated_tokens,
            HAIKU_CACHEABLE_PREFIX_FLOOR_TOKENS,
            prefix_chars,
        )
        raise IntentRouterCacheFloorError(
            f"Intent Router combined tools+system prefix is ~{estimated_tokens} "
            f"tokens ({prefix_chars} chars at ~{_PREFIX_CHARS_PER_TOKEN} "
            f"chars/token) — below Haiku 4.5's "
            f"{HAIKU_CACHEABLE_PREFIX_FLOOR_TOKENS}-token cacheable floor. "
            "Below the floor the cache_control marker is accepted by the API "
            "and silently never caches, re-billing the full prefix every turn "
            "(the epic-91 dark-spend incident). Refusing to build (No Silent "
            "Fallbacks). Either grow the prompt/schema back above the floor or "
            "deliberately remove the cache marker; re-verify with "
            "test_intent_router_prefix_token_floor_live (count_tokens)."
        )
    return _IntentRouterLlm()
