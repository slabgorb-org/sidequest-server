"""LlmClient factory — selects backend from env (ADR-073 Phase 1/2)."""

from __future__ import annotations

import os
from typing import Literal

from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.claude_client import LlmClient, LlmClientError
from sidequest.agents.tooling_protocol import ToolingLlmClient

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


class IntentRouterEmptyResponse(LlmClientError):
    """Haiku returned a response with no text content.

    Distinct from a transport error or an unparseable text payload — the
    SDK call succeeded but the model emitted no usable text (refusal,
    pause-turn, max-tokens-before-first-byte, or an unexpected
    all-non-text content array). Carries ``stop_reason``, content block
    types, and usage in the message so the failure mode is identifiable
    in logs and OTEL ``raw_preview`` instead of surfacing downstream as a
    confusing ``JSONDecodeError`` on the empty string.
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

    async def complete(self, *, system: str, user: str) -> str:
        resp = await self._sdk.messages.create(
            model=_INTENT_ROUTER_MODEL,
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=2048,
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        if not text:
            block_types = [getattr(b, "type", "?") for b in resp.content]
            usage_repr: str
            try:
                usage_repr = repr(resp.usage.model_dump())
            except Exception:  # noqa: BLE001 — usage shape varies by SDK version
                usage_repr = repr(getattr(resp, "usage", None))
            raise IntentRouterEmptyResponse(
                f"Haiku returned no text content "
                f"(stop_reason={resp.stop_reason!r}, blocks={block_types}, "
                f"usage={usage_repr})"
            )
        return text


def build_intent_router_llm() -> _IntentRouterLlm:
    """Build the Haiku-tier LLM for the Intent Router producer (ADR-113)."""
    return _IntentRouterLlm()
