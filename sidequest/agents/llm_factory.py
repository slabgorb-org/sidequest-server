"""LlmClient factory — selects backend from env (ADR-073 Phase 1/2).

Story 119-3 (ADR-101 amendment): the four single-shot Haiku sites (aside, Intent
Router, un-seeded objective classifier, archetype inference) port off the raw
``anthropic`` Messages SDK onto ``claude-agent-sdk`` over the Max subscription
pool, through the shared :func:`anthropic_sdk_client.build_agent_sdk_options`
construction seam. The three forced-extraction sites use ``output_format``
JSON-schema structured output at ``max_turns=4`` (``2`` is the mandatory FLOOR,
raised to ``4`` for headroom against intermittent ``error_max_turns`` — see
:func:`_call_haiku_sdk`; Path A — the Agent SDK has no ``tool_choice``; spec
§6.4.2); the aside is a plain no-tools completion.
:func:`_record_usage_telemetry` remains the uniform per-call usage accounting
(log line + ``llm.request`` span attributes + ``cost_usd`` + caller tag) every
Haiku call flows through.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from claude_agent_sdk import query

from sidequest.agents import cost_safety
from sidequest.agents.anthropic_cost import compute_cost_usd
from sidequest.agents.anthropic_sdk_client import (
    AnthropicSdkClient,
    _usage_int,
    build_agent_sdk_options,
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
from sidequest.telemetry.spans.llm_request import llm_request_span

if TYPE_CHECKING:
    from sidequest.agents.post_narration_classifier import ObjectiveClassifierLLM
    from sidequest.agents.sidecar_extractor import SidecarExtractorLLM
    from sidequest.genre.models.archetype_axes import BaseArchetypes
    from sidequest.genre.models.archetype_constraints import ArchetypeConstraints

logger = logging.getLogger(__name__)

# Story 119-3: ``query`` is bound at module scope (imported above) as the
# late-bound, monkeypatchable transport seam (OQ-9) — the four single-shot Haiku
# sites drive ``llm_factory.query`` so the test fleet injects a fake without a
# live subscription, mirroring the old ``build_async_anthropic`` doctrine.

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


async def _consume_to_result(prompt: str, options: Any) -> Any:
    """Drive one single-shot ``query()`` and return its terminal ResultMessage.

    Story 119-3: the Haiku single-shots have no agent loop — the stream is a
    (possibly empty) prose turn then a terminal ``ResultMessage``. Calls the
    module-level :data:`query` seam (late-bound, monkeypatchable). Raises loudly
    if no ResultMessage arrives — a call we cannot account for is the dark spend
    this discipline eliminates (No Silent Fallbacks).
    """
    result_msg: Any = None
    async for msg in query(prompt=prompt, options=options):
        if hasattr(msg, "is_error") and hasattr(msg, "num_turns"):
            result_msg = msg
    if result_msg is None:
        raise LlmClientError(
            "claude-agent-sdk query produced no terminal ResultMessage — "
            "the single-shot call cannot be accounted for (No Silent Fallbacks)."
        )
    return result_msg


def _compose_structured_system(system: str, tool_name: str, tool_description: str) -> str:
    """Fold the forced tool's name + description into the system prompt.

    Story 119-3: Path A (``output_format``) constrains the response SHAPE via the
    JSON schema but carries no tool name/description; appending them preserves
    the model's understanding of what it is producing (the forced tool's
    purpose) without touching the schema (which round-trips verbatim into
    ``output_format.schema``).
    """
    return f"{system}\n\nProduce a JSON object for the `{tool_name}` tool: {tool_description}"


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
    # Story 119-3: the agent SDK's ``ResultMessage.usage`` is a ``dict`` (spec
    # §3.2), not a usage object — read tokens dict-or-attr (``_usage_int``) so
    # the per-call cost is not silently $0 (TEA finding).
    input_tokens = _usage_int(usage, "input_tokens")
    output_tokens = _usage_int(usage, "output_tokens")
    cache_read = _usage_int(usage, "cache_read_input_tokens")
    cache_write = _usage_int(usage, "cache_creation_input_tokens")
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


async def _call_haiku_sdk(
    *,
    user: str,
    model: str,
    system_prompt: str,
    caller: str,
    session_id: str | None,
    ceiling_usd: float,
    output_format: dict[str, Any] | None = None,
) -> Any:
    """Drive one single-shot Haiku call through the shared cost-safety +
    telemetry choke point and return its terminal ``ResultMessage``.

    Story 119-6: the skeleton every single-shot Haiku site shares (extracted
    verbatim from the four 119-3 sites — aside, Intent Router, unseeded-objective
    classifier, archetype inference): pre-flight ADR-134 ceiling refusal (guarded
    by ``session_id``) → :func:`build_agent_sdk_options` at ``max_turns=4``
    (``2`` is the mandatory FLOOR; raised to ``4`` for headroom — see the
    call-site comment below) with ``allowed_tools=[]`` → ``llm_request_span`` +
    :func:`_consume_to_result` + :func:`_record_usage_telemetry` → post-call
    ledger ``record_call`` (the same ``session_id`` guard).

    Each caller supplies its OWN ``model``, ``caller`` tag, ``system_prompt`` and
    — for the three forced-extraction sites — ``output_format`` (the aside passes
    ``None`` for a plain prose completion, which leaves ``thinking`` unset; an
    ``output_format`` call auto-disables thinking in the builder). The refactor
    parameterizes these, it does not flatten them. The result-shape handling
    (``structured_output`` dict-or-raise via :func:`_extract_structured_output_or_raise`,
    or the aside's prose/``is_error`` check) stays at the call site.

    ``session_id=None`` is the explicit ADR-134 hard bypass — a sessionless
    caller opts out of the books deliberately, never by omission (No Silent
    Fallbacks). The guard lives HERE so every single-shot site bypasses
    identically.
    """
    # Pre-flight terminal refusal (ADR-134, story 91-4): a session killed by ANY
    # call site (narrator included) must not bill another token.
    if session_id is not None:
        cost_safety.ledger().check_ceiling(session_id, ceiling_usd=ceiling_usd)
    # ``max_turns=2`` is the MANDATORY FLOOR, not a ceiling: the SDK spends an
    # internal finalize turn, so ``max_turns=1`` fails closed with
    # ``error_max_turns`` (the ``max(2, ...)`` guard in build_agent_sdk_options
    # enforces it). We pass 4 for comfortable headroom — raising the value above
    # the floor only gives the ``tool-call → tool-result → finalize`` sequence
    # more room to complete. A 2026-06-19 playtest showed the structured-output
    # router pass intermittently tripping ``error_max_turns`` at mt=2
    # (intent_router.failed reason=transport, "Reached maximum number of turns (2)")
    # — the failure scales with how much the prompt gives the model to chew on, so
    # 2 is too tight for prompt-heavy router/classifier passes. This is the single
    # shared structured-output choke point: every forced-extraction caller (intent
    # router, unseeded-objective classifier, archetype inference, sidecar extractor)
    # and the prose aside route through here, so all of them gain the headroom.
    options = build_agent_sdk_options(
        model=model,
        system_prompt=system_prompt,
        max_turns=4,
        allowed_tools=[],
        output_format=output_format,
    )
    with llm_request_span(model=model) as span:
        result_msg = await _consume_to_result(user, options)
        usage = _record_usage_telemetry(span, result_msg, caller=caller, request_model=model)
    # Post-call safety pass — detector against the (session, caller) rolling
    # baselines + cumulative ceiling.
    if session_id is not None:
        cost_safety.ledger().record_call(
            session_id=session_id,
            caller=caller,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            ceiling_usd=ceiling_usd,
        )
    return result_msg


def _extract_structured_output_or_raise(
    result_msg: Any, *, error_cls: type[LlmClientError], label: str
) -> dict[str, Any]:
    """Read the forced-extraction ``structured_output`` dict off a ResultMessage,
    raising ``error_cls`` loudly when it is absent.

    Story 119-6: the dict-or-raise contract shared by the three ``output_format``
    sites (Path A, spec §6.4.2). Each site keeps its OWN loud error type via
    ``error_cls`` (:class:`IntentRouterEmptyResponse` for the router,
    :class:`LlmClientError` for the classifier/archetype) and names itself via
    ``label`` so the failure mode stays identifiable in logs / OTEL — the refactor
    parameterizes the error type, it does not collapse it to a single class. A
    ``None`` ``structured_output`` is a real condition to surface (refusal /
    pause-turn / the ``max_turns=1`` fail-closed shape), never a silently
    substituted empty dict (No Silent Fallbacks).
    """
    structured = getattr(result_msg, "structured_output", None)
    if structured is None:
        raise error_cls(
            f"{label} returned no structured_output "
            f"(subtype={getattr(result_msg, 'subtype', None)!r}, "
            f"is_error={getattr(result_msg, 'is_error', None)!r}, "
            f"num_turns={getattr(result_msg, 'num_turns', None)!r})"
        )
    return dict(structured)


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

    Drives the module-level :func:`query` transport seam with options from
    :func:`build_agent_sdk_options` — the single construction site (story
    91-1, ported to the Agent SDK over subscription auth in 119-3). Fails
    loudly if subscription auth is unavailable, and rejects a set
    ``ANTHROPIC_API_KEY`` (PAYG re-route) — No Silent Fallbacks.

    Story 91-4: carries the session identity so its Haiku spend runs the
    ADR-134 detector and feeds the per-session cumulative ceiling.
    ``session_id`` is REQUIRED keyword-only at every construction surface —
    a sessionless caller must opt out explicitly with ``session_id=None``
    (the ADR-134 hard bypass), never by omission. The ceiling env is
    parsed (fail-loud) at construction with the same validation the
    narrator client applies.
    """

    def __init__(self, *, session_id: str | None) -> None:
        self._session_id = session_id
        self._session_cost_ceiling_usd = cost_safety.parse_session_cost_ceiling_usd()

    async def complete(self, *, system: str, user: str) -> str:
        # Story 119-3: a plain no-tools subscription completion via the agent
        # SDK ``query()`` seam. No ``output_format`` (the aside returns prose,
        # not a schema). It routes through the shared ``_call_haiku_sdk`` choke
        # point, which now passes ``max_turns=4``: ``2`` is the mandatory FLOOR —
        # the SDK spends an internal finalize turn even with zero tools, so
        # ``max_turns=1`` fails closed with ``subtype='error_max_turns'`` (spec
        # §3.6/OQ-16) — and the value is raised to ``4`` for headroom against the
        # intermittent ``error_max_turns`` seen at mt=2 (2026-06-19 playtest). No
        # ANTHROPIC_API_KEY is read; a set key would re-route to PAYG and is
        # rejected loudly in ``build_agent_sdk_options`` (No Silent Fallbacks).
        #
        # Story 91-1: the call runs inside an ``llm.request`` span with the
        # uniform usage accounting (caller=aside).
        #
        # Story 91-4: pre-flight ceiling refusal (a killed session must not
        # bill another Haiku token) + post-call safety pass. ``session_id=None``
        # bypasses both, never the books.
        # Story 119-6: the cost-safety + telemetry skeleton (caller=aside) runs
        # through the shared ``_call_haiku_sdk`` choke point. No ``output_format``
        # — the aside returns prose, so the builder leaves thinking unset — and
        # the aside keeps its OWN is_error/prose handling below (it returns text,
        # not a structured dict).
        result_msg = await _call_haiku_sdk(
            user=user,
            model=_ASIDE_MODEL,
            system_prompt=system,
            caller="aside",
            session_id=self._session_id,
            ceiling_usd=self._session_cost_ceiling_usd,
        )
        if getattr(result_msg, "is_error", False):
            # A failed query (auth absent / max_turns / transport) must raise —
            # never return an empty string a caller could mistake for a real
            # "the aside had nothing to say" (No Silent Fallbacks).
            raise LlmClientError(
                "aside completion failed (is_error, "
                f"subtype={getattr(result_msg, 'subtype', None)!r}) — no PAYG fallback"
            )
        return getattr(result_msg, "result", None) or ""


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

    Same shape as :class:`_AsideLlm` — drives the module-level :func:`query`
    seam with options from :func:`build_agent_sdk_options` (the single
    construction site, story 91-1; Agent-SDK port 119-3) so the subscription
    auth check fires loudly (memory rule
    ``feedback_no_fallbacks_hard``), and carries the session identity
    (story 91-4) so the every-turn router spend — the #1 dark spender in
    the [COST-1] forensics — runs the ADR-134 detector and feeds the
    per-session cumulative ceiling.
    """

    def __init__(self, *, session_id: str | None) -> None:
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
        """Force structured extraction via ``output_format`` (Path A, spec §6.4.2).

        Story 119-3: the Agent SDK exposes no ``tool_choice``; the verified
        replacement for the raw "force one tool, read its ``.input``" mechanism
        is ``output_format={"type":"json_schema","schema": tool_schema}`` read
        from ``ResultMessage.structured_output`` — API-enforced schema-valid
        output, at ``max_turns=4`` (``2`` is the mandatory FLOOR — the +1
        finalize turn means ``max_turns=1`` fails closed with ``error_max_turns``,
        §3.6/OQ-16 — raised to ``4`` in ``_call_haiku_sdk`` for headroom after a
        2026-06-19 playtest showed the router pass intermittently tripping
        ``error_max_turns`` at mt=2). The tool name + description fold into the system prompt as
        guidance (the schema enforces the shape). The consumer's ``dict``/raise
        contract is preserved: returns the structured dict where the forced
        tool's ``.input`` went, raises :class:`IntentRouterEmptyResponse` on
        absent ``structured_output`` (the loud-raise contract).
        """
        # Story 119-6: the cost-safety + telemetry skeleton (caller=intent_router
        # — the #1 dark spender in the [COST-1] forensics) runs through the shared
        # ``_call_haiku_sdk`` choke point; the router keeps its OWN loud error type
        # (:class:`IntentRouterEmptyResponse`) for the dict-or-raise contract.
        result_msg = await _call_haiku_sdk(
            user=user,
            model=_INTENT_ROUTER_MODEL,
            system_prompt=_compose_structured_system(system, tool_name, tool_description),
            caller="intent_router",
            session_id=self._session_id,
            ceiling_usd=self._session_cost_ceiling_usd,
            output_format={"type": "json_schema", "schema": tool_schema},
        )
        return _extract_structured_output_or_raise(
            result_msg, error_cls=IntentRouterEmptyResponse, label="agent-sdk"
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
    Anthropic construction site (:func:`build_agent_sdk_options` / the
    :func:`query` seam) is never touched on this path — no Anthropic
    subscription required.

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
    Fallbacks — a typo must never silently mean Haiku).

    Story 119-5: the 91-3 build-time cache-floor guard was removed. It
    protected a ``cache_control`` marker on the raw Anthropic messages API;
    the 119-3 port runs the router over ``claude-agent-sdk`` with
    ``output_format`` and ships no cache marker, so the sub-floor trap can no
    longer spring and the SDK exposes no cache surface to re-home the guard
    onto.
    """
    try:
        backend = classification_backend()
    except UnknownClassificationBackend as exc:
        # Re-raise in the factory's typed error family so config failures
        # at this boundary are uniformly LlmClientError (same as ENV_BACKEND).
        raise UnknownBackend(str(exc)) from exc
    if backend == "ollama":
        return _OllamaIntentRouterLlm(session_id=session_id)
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
        # Story 119-3: forced extraction via ``output_format`` (Path A, §6.4.2) —
        # the Agent SDK has no ``tool_choice``; read the schema-valid dict from
        # ``ResultMessage.structured_output`` at ``max_turns=4`` (``2`` is the
        # mandatory FLOOR, raised to ``4`` for headroom in ``_call_haiku_sdk``).
        # The ``dict``/raise contract is preserved.
        # Story 119-6: the cost-safety + telemetry skeleton
        # (caller=unseeded_objective_classifier) runs through the shared
        # ``_call_haiku_sdk`` choke point; the classifier keeps its OWN loud
        # error type (:class:`LlmClientError`) for the dict-or-raise contract.
        result_msg = await _call_haiku_sdk(
            user=user,
            model=_UNSEEDED_OBJECTIVE_CLASSIFIER_MODEL,
            system_prompt=_compose_structured_system(system, tool_name, tool_description),
            caller="unseeded_objective_classifier",
            session_id=self._session_id,
            ceiling_usd=self._session_cost_ceiling_usd,
            output_format={"type": "json_schema", "schema": tool_schema},
        )
        return _extract_structured_output_or_raise(
            result_msg, error_cls=LlmClientError, label="unseeded objective classifier"
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
# Post-narration sidecar extractor (Story 151-2, ADR-150 step 2)
# ---------------------------------------------------------------------------

_SIDECAR_EXTRACTOR_MODEL = _INTENT_ROUTER_MODEL  # Haiku 4.5 (CallType.CLASSIFICATION rung)


class _SidecarExtractorLlm:
    """Single-shot Haiku adapter for the post-narration sidecar extractor (151-2).

    Satisfies ``sidecar_extractor.SidecarExtractorLLM`` — the same ``emit_tool``
    contract as :class:`_IntentRouterLlm` / :class:`_UnseededObjectiveClassifierLlm`.
    Runs on the live ``CallType.CLASSIFICATION → claude-haiku-4-5`` rung; no new
    model-routing, transport, or protocol infrastructure (ADR-150 §Decision). It
    fires once per turn in SHADOW mode, so cost is recorded under caller
    ``sidecar_extraction`` to attribute it distinctly from the every-turn router
    spend in the [COST-1] forensics.
    """

    def __init__(self, *, session_id: str | None) -> None:
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
        # Forced extraction via ``output_format`` (Path A, §6.4.2) — the Agent SDK
        # has no ``tool_choice``; read the schema-valid dict from
        # ``ResultMessage.structured_output`` at ``max_turns=4`` (``2`` is the
        # mandatory FLOOR, raised to ``4`` for headroom in ``_call_haiku_sdk``).
        # The dict/raise contract is preserved; the extractor's retry/failure taxonomy owns the
        # raise (it catches the transport boundary, retries once, then surfaces
        # SidecarExtractionFailure).
        result_msg = await _call_haiku_sdk(
            user=user,
            model=_SIDECAR_EXTRACTOR_MODEL,
            system_prompt=_compose_structured_system(system, tool_name, tool_description),
            caller="sidecar_extraction",
            session_id=self._session_id,
            ceiling_usd=self._session_cost_ceiling_usd,
            output_format={"type": "json_schema", "schema": tool_schema},
        )
        return _extract_structured_output_or_raise(
            result_msg, error_cls=LlmClientError, label="sidecar extractor"
        )


def build_sidecar_extractor_llm(*, session_id: str | None) -> SidecarExtractorLLM:
    """Build the Haiku adapter for the post-narration sidecar extractor (151-2).

    Returns the public ``SidecarExtractorLLM`` Protocol the extractor consumes —
    callers depend on the contract, not the private adapter class. ``session_id``
    is required keyword-only (supply the room slug or opt out with ``None``) so
    the per-turn shadow-extraction spend runs the ADR-134 detector and feeds the
    per-session ceiling, the same discipline as the Intent Router and the
    un-seeded objective classifier.
    """
    return _SidecarExtractorLlm(session_id=session_id)


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
    via ``record_call`` under caller ``archetype_inference``. The transport
    comes from the module-level :func:`query` seam with options from
    :func:`build_agent_sdk_options` (story 91-1 single construction site,
    late-bound through module globals so the test fake is what this
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

    # Story 119-6: the pre-flight ADR-134 ceiling refusal (a killed session must
    # not bill another inference token) now lives inside ``_call_haiku_sdk`` —
    # parsed here, enforced there before the SDK query fires. The empty-freeform
    # short-circuit above already guarantees no spend for the no-fodder case.
    ceiling_usd = cost_safety.parse_session_cost_ceiling_usd()

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

    # Story 119-3: forced extraction via ``output_format`` (Path A, §6.4.2) —
    # the Agent SDK has no ``tool_choice``; read the enum-constrained dict from
    # ``ResultMessage.structured_output`` at ``max_turns=4`` (``2`` is the
    # mandatory FLOOR, raised to ``4`` for headroom in ``_call_haiku_sdk``). The
    # tool's name/description fold into the system prompt; the schema (with its
    # per-axis ``enum``) rides ``output_format``.
    # Story 119-6: the cost-safety + telemetry skeleton (caller=archetype_inference)
    # runs through the shared ``_call_haiku_sdk`` choke point; the archetype keeps
    # its OWN loud error type (:class:`LlmClientError`) + the enum validation below.
    result_msg = await _call_haiku_sdk(
        user=user,
        model=_ARCHETYPE_INFERENCE_MODEL,
        system_prompt=_compose_structured_system(
            _ARCHETYPE_INFERENCE_SYSTEM,
            _ARCHETYPE_INFERENCE_TOOL_NAME,
            "Report the inferred archetype axis value(s). Omit any axis you "
            "cannot infer from the player's answers.",
        ),
        caller="archetype_inference",
        session_id=session_id,
        ceiling_usd=ceiling_usd,
        output_format={"type": "json_schema", "schema": tool_schema},
    )
    parsed: dict[str, Any] = _extract_structured_output_or_raise(
        result_msg, error_cls=LlmClientError, label="archetype inference"
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
