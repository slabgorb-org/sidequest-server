"""LlmClient factory — selects backend from env (ADR-073 Phase 1/2)."""

from __future__ import annotations

import os

from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.claude_client import ClaudeClient, LlmClient, LlmClientError
from sidequest.agents.ollama_client import DEFAULT_OLLAMA_URL, OllamaClient
from sidequest.agents.tooling_protocol import ToolingLlmClient

ENV_BACKEND = "SIDEQUEST_LLM_BACKEND"
ENV_OLLAMA_URL = "SIDEQUEST_OLLAMA_URL"

_VALID_BACKENDS = frozenset({"claude", "ollama", "anthropic_sdk"})


class UnknownBackend(LlmClientError):
    """SIDEQUEST_LLM_BACKEND value was not one of the supported backends."""


def build_llm_client() -> LlmClient | ToolingLlmClient:
    """Return the configured LlmClient. Default: AnthropicSdkClient (Phase D).

    Returns LlmClient for 'claude'/'ollama' backends and ToolingLlmClient for
    'anthropic_sdk'. ToolingLlmClient is a richer protocol (tool-use loop,
    prompt caching) and intentionally does not inherit from LlmClient — the
    two protocols serve different call sites. Callers that need tool-use must
    isinstance-check or accept the union type.

    Fails loudly for unknown backend values — no silent fallback (CLAUDE.md).
    """
    raw = os.environ.get(ENV_BACKEND, "anthropic_sdk")
    key = raw.strip().lower()
    if key not in _VALID_BACKENDS:
        raise UnknownBackend(
            f"{ENV_BACKEND}={raw!r} not supported; pick one of {sorted(_VALID_BACKENDS)}"
        )
    if key == "claude":
        return ClaudeClient()
    if key == "ollama":
        base_url = os.environ.get(ENV_OLLAMA_URL, DEFAULT_OLLAMA_URL)
        return OllamaClient(base_url=base_url)
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

    async def complete(self, *, system: str, user: str) -> str:
        resp = await self._sdk.messages.create(
            model=_INTENT_ROUTER_MODEL,
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=2048,
        )
        return "".join(block.text for block in resp.content if block.type == "text")


def build_intent_router_llm() -> _IntentRouterLlm:
    """Build the Haiku-tier LLM for the Intent Router producer (ADR-113)."""
    return _IntentRouterLlm()
