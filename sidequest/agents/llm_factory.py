"""LlmClient factory — selects backend from env (ADR-073 Phase 1/2)."""

from __future__ import annotations

import os
from typing import Any, Literal

from sidequest.agents.anthropic_sdk_client import (
    _EXTENDED_CACHE_TTL_BETA,
    AnthropicSdkClient,
)
from sidequest.agents.claude_client import LlmClient, LlmClientError
from sidequest.agents.tooling_protocol import ToolingLlmClient
from sidequest.telemetry.spans.llm_request import llm_request_span

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

    Lazily constructs an ``AsyncAnthropic`` (same SDK the narrator uses).
    Fails loudly if ``ANTHROPIC_API_KEY`` is unset — No Silent Fallbacks.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LlmClientError(
                "ANTHROPIC_API_KEY not set — required to resolve player "
                "asides (ADR-107). No silent fallback."
            )
        from anthropic import AsyncAnthropic

        self._sdk = AsyncAnthropic(api_key=api_key)

    async def complete(self, *, system: str, user: str) -> str:
        # ``system`` stays a BARE string — NOT a cached content block. The
        # aside prompt is ~361 tokens, far below Haiku 4.5's 4,096-token
        # cacheable-prefix floor, so a ``cache_control`` marker here is
        # accepted by the API but silently never caches. Adding one would
        # imply caching that does not happen (No Silent Fallbacks). The
        # Intent Router (``_IntentRouterLlm``) is the every-turn cost driver
        # and clears the floor — that is where caching pays off.
        resp = await self._sdk.messages.create(
            model=_ASIDE_MODEL,
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=512,
        )
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


def _record_haiku_usage_on_span(span: Any, resp: Any) -> None:
    """Stamp token usage onto an ``llm.request`` span (OTEL Observability
    Principle). ``cached_input_read_tokens`` is the lie-detector field — it goes
    non-zero on turn 2+ once the static prefix is warm, proving the cache
    actually engaged rather than Claude just claiming a cheap turn."""
    usage = getattr(resp, "usage", None)
    span.set_attribute("llm.input_tokens", int(getattr(usage, "input_tokens", 0) or 0))
    span.set_attribute("llm.output_tokens", int(getattr(usage, "output_tokens", 0) or 0))
    span.set_attribute(
        "llm.cached_input_read_tokens",
        int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )
    span.set_attribute(
        "llm.cached_input_write_tokens",
        int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )
    stop_reason = getattr(resp, "stop_reason", None)
    if stop_reason:
        span.set_attribute("llm.stop_reason", str(stop_reason))


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

    Same shape as :class:`_AsideLlm` — eagerly constructs an
    ``AsyncAnthropic`` so the build-time environment check fires loudly
    (memory rule ``feedback_no_fallbacks_hard``).
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LlmClientError(
                "ANTHROPIC_API_KEY not set — required for the Intent Router "
                "producer (ADR-113). No silent fallback."
            )
        from anthropic import AsyncAnthropic

        self._sdk = AsyncAnthropic(api_key=api_key)

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
            _record_haiku_usage_on_span(span, resp)
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
    """Build the Haiku-tier LLM for the Intent Router producer (ADR-113)."""
    return _IntentRouterLlm()
