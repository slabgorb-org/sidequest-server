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
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from sidequest.agents import cost_safety
from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.anthropic_sdk_client import (
    _EXTENDED_CACHE_TTL_BETA,
    AnthropicSdkClient,
)
from sidequest.agents.claude_client import LlmClient, LlmClientError

# ENV_CLASSIFICATION_BACKEND is re-exported on purpose (explicit `as` alias):
# operators and tests reach the classification seam through this factory
# module, alongside ENV_BACKEND / ENV_OLLAMA_URL.
from sidequest.agents.model_routing import (
    ENV_CLASSIFICATION_BACKEND as ENV_CLASSIFICATION_BACKEND,
)
from sidequest.agents.model_routing import (
    LOCAL_CLASSIFIER_MODEL,
    UnknownClassificationBackend,
    classification_backend,
)
from sidequest.agents.ollama_client import DEFAULT_OLLAMA_URL, OllamaClient
from sidequest.agents.tooling_protocol import ToolingLlmClient
from sidequest.telemetry.spans.intent_router import intent_router_cache_floor_span
from sidequest.telemetry.spans.llm_request import llm_request_span

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from sidequest.agents.post_narration_classifier import ObjectiveClassifierLLM
    from sidequest.genre.models.archetype_axes import BaseArchetypes
    from sidequest.genre.models.archetype_constraints import ArchetypeConstraints

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


class _UsageSummary(NamedTuple):
    """The accounted shape of one SDK call — returned by
    ``_record_usage_telemetry`` so the cost-safety pass (story 91-4)
    reuses the figures already computed for the books instead of
    re-deriving them."""

    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


def _record_usage_telemetry(
    span: Any,
    resp: Any,
    *,
    caller: str,
    request_model: str,
) -> _UsageSummary:
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
    return _UsageSummary(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
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

    Story 91-4: carries the session identity so its Haiku spend runs the
    ADR-134 detector and feeds the per-session cumulative ceiling.
    ``session_id`` is REQUIRED keyword-only at every construction surface —
    a sessionless caller must opt out explicitly with ``session_id=None``
    (the ADR-134 hard bypass), never by omission. The ceiling env is
    parsed (fail-loud) at construction with the same validation the
    narrator client applies.
    """

    def __init__(self, *, session_id: str | None) -> None:
        self._sdk = build_async_anthropic()
        self._session_id = session_id
        self._session_cost_ceiling_usd = cost_safety.parse_session_cost_ceiling_usd()

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
        #
        # Story 91-4: pre-flight ceiling refusal (a killed session must not
        # bill another Haiku token) + post-call safety pass (detector +
        # cumulative). ``session_id=None`` bypasses both, never the books.
        if self._session_id is not None:
            cost_safety.ledger().check_ceiling(
                self._session_id, ceiling_usd=self._session_cost_ceiling_usd
            )
        with llm_request_span(model=_ASIDE_MODEL) as span:
            resp = await self._sdk.messages.create(
                model=_ASIDE_MODEL,
                system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=512,
            )
            usage = _record_usage_telemetry(span, resp, caller="aside", request_model=_ASIDE_MODEL)
        if self._session_id is not None:
            cost_safety.ledger().record_call(
                session_id=self._session_id,
                caller="aside",
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
                ceiling_usd=self._session_cost_ceiling_usd,
            )
        return "".join(block.text for block in resp.content if block.type == "text")


def build_aside_llm(*, session_id: str | None) -> _AsideLlm:
    """Build the Haiku-tier LLM for out-of-band aside resolution (ADR-107).

    Story 91-4: ``session_id`` is required keyword-only — the caller must
    either supply the canonical session id (the room slug) or explicitly
    opt out with ``None``. Omission is a ``TypeError`` so a future call
    site cannot silently construct an uncovered Haiku spender.
    """
    return _AsideLlm(session_id=session_id)


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
    ``feedback_no_fallbacks_hard``), and carries the session identity
    (story 91-4) so the every-turn router spend — the #1 dark spender in
    the [COST-1] forensics — runs the ADR-134 detector and feeds the
    per-session cumulative ceiling.
    """

    def __init__(self, *, session_id: str | None) -> None:
        self._sdk = build_async_anthropic()
        self._session_id = session_id
        self._session_cost_ceiling_usd = cost_safety.parse_session_cost_ceiling_usd()

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
        # Story 91-4: pre-flight ceiling refusal — a session killed by ANY
        # call site (narrator included) must not bill another router token.
        if self._session_id is not None:
            cost_safety.ledger().check_ceiling(
                self._session_id, ceiling_usd=self._session_cost_ceiling_usd
            )
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
            usage = _record_usage_telemetry(
                span, resp, caller="intent_router", request_model=_INTENT_ROUTER_MODEL
            )
        # Story 91-4: post-call safety pass — detector against the
        # (session, intent_router) rolling baselines + cumulative ceiling.
        if self._session_id is not None:
            cost_safety.ledger().record_call(
                session_id=self._session_id,
                caller="intent_router",
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
                ceiling_usd=self._session_cost_ceiling_usd,
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


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the single JSON object out of a prompt-coerced completion.

    Story 92-2 production twin of the harness extractor: a missing or
    unparseable object RAISES — the router's retry/failure taxonomy owns
    the failure. Never silently substitute an empty package.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise IntentRouterEmptyResponse(f"local classifier returned no JSON object: {text[:160]!r}")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise IntentRouterEmptyResponse(f"local classifier JSON failed to parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise IntentRouterEmptyResponse(
            f"local classifier JSON is {type(parsed).__name__}, expected object"
        )
    return parsed


def build_local_classifier_client() -> OllamaClient:
    """Build the OllamaClient serving the local classification rung (92-2).

    Shared by the Intent Router adapter (CLASSIFICATION) and the dungeon
    curate stage (SCRATCH — ``materializer.py``): base URL from
    ``SIDEQUEST_OLLAMA_URL``, model map pinned to the identity entry for
    :data:`LOCAL_CLASSIFIER_MODEL` (the ladder hands callers a concrete
    model id, not a sonnet/haiku hint, so the hint resolves to itself).
    Transport errors surface as ``OllamaClientError`` — fail loud, no
    fallback to Anthropic.
    """
    base_url = os.environ.get(ENV_OLLAMA_URL, DEFAULT_OLLAMA_URL)
    return OllamaClient(
        base_url=base_url,
        model_map={LOCAL_CLASSIFIER_MODEL: LOCAL_CLASSIFIER_MODEL},
    )


class _OllamaIntentRouterLlm:
    """Production Ollama-backed ``IntentRouterLLM`` adapter (story 92-2).

    The promised production twin of the harness's measurement-only
    ``QwenRouterLlm`` (its docstring names this story). qwen has no native
    forced-tool path (``OllamaClient.capabilities()`` reports
    ``supports_tools=False``), so the tool schema is embedded in the system
    prompt and the raw completion is parsed as a single JSON object — the
    same prompt-coercion the 92-1 A/B gate evidence measured.

    Reuses the existing ``ollama_client`` transport (Don't Reinvent — Wire
    Up What Exists): base URL from ``SIDEQUEST_OLLAMA_URL``, model pinned to
    :data:`LOCAL_CLASSIFIER_MODEL` (the A/B-validated id). Transport errors
    surface as ``OllamaClientError`` — an unreachable Ollama fails the turn
    LOUDLY; there is NO fallback to Haiku, ever (a silent fallback would
    recreate the exact dark spend epic 92 eliminates, by design). The
    Anthropic construction site (:func:`build_async_anthropic`) is never
    touched on this path — no ``ANTHROPIC_API_KEY`` required.

    ``session_id`` is carried for signature parity with the Haiku adapter
    (story 91-4 keyword-only contract) but the ADR-134 cost ledger is not
    wired: local calls bill $0, and the ceiling protects money, not compute.
    OTEL: the underlying ``OllamaClient`` emits ``agent.backend=ollama``
    spans on every call — story 92-4's playtest verification consumes them.
    """

    def __init__(self, *, session_id: str | None) -> None:
        self._session_id = session_id
        self._client = build_local_classifier_client()

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        coercion = (
            f"\n\nYou cannot call tools. Instead, respond with ONLY a single "
            f"JSON object that is a valid input for the `{tool_name}` tool "
            f"({tool_description}). The JSON Schema is:\n"
            f"{json.dumps(tool_schema)}\n"
            f"No prose, no markdown fences — the JSON object only."
        )
        # Review rework [SEC HIGH]: route through ``send_with_session`` (role-
        # separated ``/api/chat`` messages array) NOT ``send_stateless`` (which
        # flattens system+user into one undivided prompt). Player-authored text
        # must stay in the ``role: user`` turn, never adjacent to the JSON-
        # coercion instructions — the flattened form raised both the prompt-
        # injection surface and the misclassification rate. ``session_id=None``
        # keeps each classification a fresh stateless turn.
        resp = await self._client.send_with_session(
            prompt=user,
            system_prompt=system + coercion,
            session_id=None,
            model=LOCAL_CLASSIFIER_MODEL,
        )
        return _extract_json_object(resp.text)


def build_intent_router_llm(*, session_id: str | None) -> _IntentRouterLlm | _OllamaIntentRouterLlm:
    """Build the Haiku-tier LLM for the Intent Router producer (ADR-113).

    Story 91-4: ``session_id`` is required keyword-only — supply the
    canonical session id (the room slug) or opt out explicitly with
    ``None``. Omission is a ``TypeError``: the router is the highest-
    frequency Haiku caller and must never silently run uncovered.

    Story 92-2 local rung: when ``SIDEQUEST_CLASSIFICATION_BACKEND=ollama``
    (explicit config — the default is unchanged Haiku), returns the
    Ollama-backed :class:`_OllamaIntentRouterLlm` instead. An unknown env
    value raises :class:`UnknownBackend` naming the env var (No Silent
    Fallbacks — a typo must never silently mean Haiku). The 91-3 cache-floor
    guard below is HAIKU-ONLY: it protects an Anthropic cache, and the local
    path has none (this is also what frees 82-10's prompt slimming).

    Story 91-3 fail-loud floor guard: validates the combined tools+system
    cacheable prefix against Haiku 4.5's
    :data:`HAIKU_CACHEABLE_PREFIX_FLOOR_TOKENS` BEFORE constructing the
    adapter, and raises :class:`IntentRouterCacheFloorError` if the estimate
    is sub-floor — below the floor the adapter's ``cache_control`` marker is
    accepted by the API and silently never caches (the epic-91 dark-spend
    incident). The decision is emitted as an ``intent_router.cache_floor``
    span on both paths so the GM panel can verify the guard engaged (OTEL
    Observability Principle). The guard runs BEFORE the adapter is built so a
    sub-floor prefix fails loud regardless of ``session_id``.
    """
    try:
        backend = classification_backend()
    except UnknownClassificationBackend as exc:
        # Re-raise in the factory's typed error family so config failures
        # at this boundary are uniformly LlmClientError (same as ENV_BACKEND).
        raise UnknownBackend(str(exc)) from exc
    if backend == "ollama":
        return _OllamaIntentRouterLlm(session_id=session_id)
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
    return _IntentRouterLlm(session_id=session_id)


# ---------------------------------------------------------------------------
# Un-seeded objective classifier (Story 117-6)
# ---------------------------------------------------------------------------

_UNSEEDED_OBJECTIVE_CLASSIFIER_MODEL = _INTENT_ROUTER_MODEL  # Haiku 4.5


class _UnseededObjectiveClassifierLlm:
    """Single-shot Haiku adapter for the un-seeded objective classifier (117-6).

    Satisfies ``post_narration_classifier.ObjectiveClassifierLLM`` — the same
    ``emit_tool`` contract as :class:`_IntentRouterLlm`, but sends a BARE-STRING
    ``system`` (no ``cache_control``): the classifier's prompt is far below Haiku's
    cacheable floor, where a cache marker is accepted by the API and silently never
    caches (the epic-91 dark-spend trap). It fires at most once per turn and only on
    objective-bearing narration (the watcher gates it), so per-turn cache reuse is
    not the win — not re-billing a phantom cache is. Cost is recorded under caller
    ``unseeded_objective_classifier`` so the [COST-1] forensics attribute it
    distinctly from the every-turn router spend.
    """

    def __init__(self, *, session_id: str | None) -> None:
        self._sdk = build_async_anthropic()
        self._session_id = session_id
        self._session_cost_ceiling_usd = cost_safety.parse_session_cost_ceiling_usd()

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        # Pre-flight ceiling refusal (ADR-134): a session killed by ANY call site
        # must not bill another classification token.
        if self._session_id is not None:
            cost_safety.ledger().check_ceiling(
                self._session_id, ceiling_usd=self._session_cost_ceiling_usd
            )
        with llm_request_span(model=_UNSEEDED_OBJECTIVE_CLASSIFIER_MODEL) as span:
            resp = await self._sdk.messages.create(
                model=_UNSEEDED_OBJECTIVE_CLASSIFIER_MODEL,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[
                    {
                        "name": tool_name,
                        "description": tool_description,
                        "input_schema": tool_schema,
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
                max_tokens=256,
            )
            usage = _record_usage_telemetry(
                span,
                resp,
                caller="unseeded_objective_classifier",
                request_model=_UNSEEDED_OBJECTIVE_CLASSIFIER_MODEL,
            )
        if self._session_id is not None:
            cost_safety.ledger().record_call(
                session_id=self._session_id,
                caller="unseeded_objective_classifier",
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
                ceiling_usd=self._session_cost_ceiling_usd,
            )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return dict(block.input)
        block_types = [getattr(b, "type", "?") for b in resp.content]
        raise LlmClientError(
            f"unseeded objective classifier returned no tool_use block "
            f"(stop_reason={getattr(resp, 'stop_reason', None)!r}, blocks={block_types})"
        )


def build_unseeded_objective_classifier_llm(*, session_id: str | None) -> ObjectiveClassifierLLM:
    """Build the Haiku adapter for the un-seeded objective classifier (Story 117-6).

    Returns the public ``ObjectiveClassifierLLM`` Protocol the classifier consumes —
    callers depend on the contract, not the private adapter class.

    ``session_id`` is required keyword-only (supply the room slug or opt out with
    ``None``) so the post-narration classification spend runs the ADR-134 detector
    and feeds the per-session ceiling, same discipline as the Intent Router.
    """
    return _UnseededObjectiveClassifierLlm(session_id=session_id)


# ---------------------------------------------------------------------------
# Chargen archetype inference (Story 93-1)
# ---------------------------------------------------------------------------

_ARCHETYPE_INFERENCE_MODEL = _INTENT_ROUTER_MODEL
_ARCHETYPE_INFERENCE_TOOL_NAME = "infer_archetype_axes"
# Review rework (python.md #11): the joined freeform fodder is player-
# authored and otherwise unbounded (the WS frame cap is ~16MB). Bound it
# before the SDK call so a hostile/verbose player can neither grind
# per-call cost nor draw a context-length 400. 4,000 chars (~1k tokens)
# comfortably holds several paragraphs of backstory — the archetype
# signal is in the first paragraphs, not the fortieth.
_ARCHETYPE_INFERENCE_MAX_FODDER_CHARS = 4_000
_ARCHETYPE_INFERENCE_SYSTEM = (
    "You infer a tabletop RPG character's archetype axes from the player's "
    "own freeform character-creation answers. Read the answers, then call "
    "the tool with the requested missing axis value(s). Pick the value that "
    "best fits the character the player described. You MUST pick values "
    "from the allowed lists only — never invent a value. Omit an axis "
    "entirely if the text gives you no basis to infer it."
)


async def infer_archetype_from_freeform(
    *,
    freeform_text: str,
    base: BaseArchetypes,
    constraints: ArchetypeConstraints,
    existing_hints: dict[str, str | None],
    session_id: str | None,
) -> dict[str, str] | None:
    """Infer missing archetype axis value(s) from freeform chargen answers.

    Story 93-1 ([BAR-1]): the all-freeform chargen path accumulates zero
    ``jungian_hint``/``rpg_role_hint``, so axis-bearing packs dead-end at the
    45-6 gate. This single-shot Haiku call (intent-router ``emit_tool``
    pattern, ADR-102/ADR-113) reads the player's accumulated freeform text
    and fills ONLY the missing axes, constrained to the pack's valid ids
    (``base.jungian[*].id`` / ``base.rpg_roles[*].id``).

    Returns:
        - ``dict`` containing ONLY newly-inferred axes (never echoes or
          overrides an existing hint),
        - ``{}`` when nothing is missing or Haiku declined every missing
          axis (caller keeps the existing fail-loud block),
        - ``None`` when the freeform text is empty/whitespace (nothing to
          infer from — no SDK spend) or Haiku returned an out-of-enum value
          (the WHOLE inference is invalid: no coercion, no partial accept,
          no pack-default fallback — No Silent Fallbacks).

    Cost (ADR-134): pre-flight ``check_ceiling`` refuses a killed session
    before a single token is billed; the call records to the session ledger
    via ``record_call`` under caller ``archetype_inference``. The SDK comes
    from :func:`build_async_anthropic` (story 91-1 single construction
    site, late-bound through module globals so the test fake is what this
    function receives).

    The bare-string ``system`` is deliberate: this fires at most once per
    chargen, and the prefix is far below Haiku's cacheable floor — a
    ``cache_control`` marker here would be accepted and silently never
    cache (the epic-91 trap).
    """
    valid_jungian = [j.id for j in base.jungian]
    valid_roles = [r.id for r in base.rpg_roles]
    axis_enums: dict[str, list[str]] = {
        "jungian_hint": valid_jungian,
        "rpg_role_hint": valid_roles,
    }
    missing = [
        (axis, enum)
        for axis, enum in axis_enums.items()
        if existing_hints.get(axis) is None and enum
    ]
    if not missing:
        return {}
    if not freeform_text.strip():
        logger.info(
            "chargen.archetype_inference skipped reason=empty_freeform session_id=%s",
            session_id,
        )
        return None
    if len(freeform_text) > _ARCHETYPE_INFERENCE_MAX_FODDER_CHARS:
        # Review rework (python.md #11): bound player-authored input before
        # the API call. Loud, not silent — the player's words were cut.
        logger.warning(
            "chargen.archetype_inference fodder truncated from %d to %d chars session_id=%s",
            len(freeform_text),
            _ARCHETYPE_INFERENCE_MAX_FODDER_CHARS,
            session_id,
        )
        freeform_text = freeform_text[:_ARCHETYPE_INFERENCE_MAX_FODDER_CHARS]

    ceiling_usd = cost_safety.parse_session_cost_ceiling_usd()
    if session_id is not None:
        # Pre-flight terminal refusal (ADR-134): a session killed by ANY
        # call site must not bill another inference token.
        cost_safety.ledger().check_ceiling(session_id, ceiling_usd=ceiling_usd)

    missing_axis_names = [axis for axis, _ in missing]
    tool_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            axis: {
                "type": "string",
                "enum": enum,
                "description": f"The inferred {axis} — one of the allowed values.",
            }
            for axis, enum in missing
        },
        "required": [],
        "additionalProperties": False,
    }
    constraint_lines = ""
    if constraints.valid_pairings.common:
        common = ", ".join(f"{j}/{r}" for j, r in constraints.valid_pairings.common)
        constraint_lines = f"\n\nCommon pairings in this genre (prefer one of these): {common}"
    # ADR-047 defense-in-depth: the player's words ride inside a structural
    # delimiter so instruction-shaped text in their answers reads as quoted
    # material. The forced tool_choice + enum validation remain the actual
    # security boundary; this just hardens the prompt shape.
    user = (
        f"Missing axes to infer: {missing_axis_names}\n\n"
        "Player's character-creation answers (their own words):\n"
        f"<player_answers>\n{freeform_text}\n</player_answers>"
        f"{constraint_lines}"
    )

    # Late-bound through module globals (story 91-1 monkeypatch doctrine).
    sdk = build_async_anthropic()
    with llm_request_span(model=_ARCHETYPE_INFERENCE_MODEL) as span:
        resp = await sdk.messages.create(
            model=_ARCHETYPE_INFERENCE_MODEL,
            system=_ARCHETYPE_INFERENCE_SYSTEM,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": _ARCHETYPE_INFERENCE_TOOL_NAME,
                    "description": (
                        "Report the inferred archetype axis value(s). Omit any "
                        "axis you cannot infer from the player's answers."
                    ),
                    "input_schema": tool_schema,
                }
            ],
            tool_choice={"type": "tool", "name": _ARCHETYPE_INFERENCE_TOOL_NAME},
            max_tokens=256,
        )
        usage = _record_usage_telemetry(
            span,
            resp,
            caller="archetype_inference",
            request_model=_ARCHETYPE_INFERENCE_MODEL,
        )
    if session_id is not None:
        cost_safety.ledger().record_call(
            session_id=session_id,
            caller="archetype_inference",
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            ceiling_usd=ceiling_usd,
        )

    parsed: dict[str, Any] | None = None
    for block in resp.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == _ARCHETYPE_INFERENCE_TOOL_NAME
        ):
            parsed = dict(block.input)
            break
    if parsed is None:
        block_types = [getattr(b, "type", "?") for b in resp.content]
        raise LlmClientError(
            f"archetype inference returned no tool_use block "
            f"(stop_reason={getattr(resp, 'stop_reason', None)!r}, blocks={block_types})"
        )

    inferred: dict[str, str] = {}
    for axis, enum in missing:
        value = parsed.get(axis)
        if value is None:
            continue
        if value not in enum:
            # Out-of-enum invalidates the WHOLE inference — no coercion,
            # no partial accept (No Silent Fallbacks).
            logger.warning(
                "chargen.archetype_inference_invalid axis=%s value=%r "
                "session_id=%s — out-of-enum, rejecting inference",
                axis,
                value,
                session_id,
            )
            return None
        inferred[axis] = str(value)
    return inferred
