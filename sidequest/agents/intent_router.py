"""intent_router — Production live-path producer for the Intent Router
engagement spine (ADR-113).

The Intent Router is the pre-narrator pass that reads each submitted player
action plus a state summary, asks Haiku (via the Anthropic SDK) to infer
intent as confidence-scored advisory dispatches, and emits a
``DispatchPackage``. Downstream, ``run_dispatch_bank`` engages the matching
mechanical engines on the canonical snapshot BEFORE the narrator runs — so
the narrator narrates already-real state rather than self-reporting
engagement via unreliable sidecar fields.

Originally shipped 2026-04-23 as ``local_dm.py`` (the LocalDM decomposer,
``claude -p`` subprocess backed). Shelved 2026-04-28 because the subprocess
doubled per-turn latency. ADR-113 (2026-05-23) reverses the shelving on the
SDK path: Haiku via the Anthropic SDK is a cheap single API call sharing the
narrator's transport, so the producer is back on the live turn path. The
``claude -p`` legacy backend stays under ADR-013.

Failure policy: no silent fallbacks (memory rule
``feedback_no_fallbacks_hard`` + CLAUDE.md "No Silent Fallbacks"). On
timeout / transport error / unparseable / schema-invalid output, this
module emits an ERROR-level ``intent_router.failed`` OTEL span, attempts
ONE bounded retry, and raises :class:`IntentRouterFailure` if the retry
also fails. There is no degraded-shape ``DispatchPackage`` return path.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from pydantic import ValidationError

from sidequest.agents.llm_factory import (
    _INTENT_ROUTER_MODEL,
    IntentRouterEmptyResponse,
)
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.telemetry.spans.intent_router import (
    intent_router_decompose_span,
    intent_router_failed_span,
)

logger = logging.getLogger(__name__)

_RAW_PREVIEW_LIMIT = 160
_MAX_TOTAL_ATTEMPTS = 2  # First attempt + one bounded retry.


class IntentRouterFailure(Exception):
    """The Intent Router producer failed after the bounded retry.

    Raised explicitly so the orchestrator (wired in 59-4) can surface the
    failure rather than silently degrading to narrator-only continuation.
    Carries the human-readable reason of the LAST failed attempt.
    """


class IntentRouterLLM(Protocol):
    """Single-shot LLM contract the router consumes.

    Mirrors the ``AsideLLM`` Protocol at ``sidequest/agents/aside_resolver.py``
    — one async ``complete`` method, no session state. The SDK-Haiku
    adapter in ``llm_factory.py`` is the live implementation; tests inject
    an ``AsyncMock``.
    """

    async def complete(self, *, system: str, user: str) -> str: ...


_SYSTEM_PROMPT = """You are the Intent Router — an impartial structured-output reader.

Your job: read a player's action + the game state summary, then emit ONE JSON
object matching the DispatchPackage schema. Never write prose. Never call
tools. Output JSON only — no preamble, no explanation, no markdown fences.

For each player action:
  1. Resolve referents (pronouns, ellipses, demonstratives). Every resolution
     carries a confidence 0.0-1.0 and plausible alternatives. If nothing
     plausibly resolves, set resolved_to=null with confidence=0 — do NOT
     invent a filler.
  2. Emit subsystem dispatches keyed on the action's mechanical intent.
     Available subsystem keys:
       - confrontation: structured encounter (combat, negotiation, chase, etc.)
       - magic_working: spell or magical ability usage
       - scenario_clue: clue/evidence discovery or advancement
       - npc_agency: NPC reacts based on established role and disposition
       - distinctive_detail_hint: name a referent by its distinctive detail
       - reflect_absence: player addresses someone/something not present
  3. Emit narrator_instructions — must_narrate / must_not_narrate /
     distinctive_detail_for_referent / canonical_only_do_not_reveal_to_others.
  4. Set confidence_global to your overall confidence across the turn.

Every dispatch carries a visibility tag. Default visible_to="all" with empty
perception_fidelity unless the state clearly names asymmetric visibility.

Pydantic rejects unknown fields. Stay inside the schema. Output valid JSON
only — no preamble, no code fences, no commentary."""


def _build_user_prompt(action: str, state_summary: Any) -> str:
    if isinstance(state_summary, str):
        state_text = state_summary
    else:
        state_text = json.dumps(state_summary, default=str, sort_keys=True)
    return (
        f"<game_state>\n{state_text}\n</game_state>\n"
        f"<raw_action>\n{action}\n</raw_action>\n"
        f"Emit DispatchPackage JSON for this single action."
    )


def _count_dispatches(pkg: DispatchPackage) -> int:
    count = sum(len(pd.dispatch) for pd in pkg.per_player)
    count += sum(len(ca.dispatch) for ca in pkg.cross_player)
    return count


class IntentRouter:
    """Live-path Intent Router producer.

    Constructor takes an injected :class:`IntentRouterLLM` so the SDK
    backend can be swapped (ADR-073 future: local fine-tuned model)
    without rewriting the router. Tests inject mocks; the live path uses
    :func:`sidequest.agents.llm_factory.build_intent_router_llm`.

    The class is stateless — every ``decompose`` call sends the full
    system prompt and a fresh user prompt. There is no session id, no
    persistent context across turns.
    """

    def __init__(self, *, llm: IntentRouterLLM) -> None:
        self._llm = llm

    async def decompose(
        self,
        *,
        action: str,
        state_summary: Any,
    ) -> DispatchPackage:
        """Decompose a player action into a :class:`DispatchPackage`.

        Raises :class:`IntentRouterFailure` if both the initial attempt
        and the bounded retry fail. There is no degraded-shape return
        path: failure surfaces as an exception (per ADR-113 and memory
        rule ``feedback_no_fallbacks_hard``).
        """
        user_prompt = _build_user_prompt(action, state_summary)
        action_length = len(action)
        start_ns = time.perf_counter_ns()
        last_failure: tuple[str, str] | None = None

        for attempt_index in range(_MAX_TOTAL_ATTEMPTS):
            retry_count = attempt_index  # 0 on first try, 1 on retry.
            try:
                raw_text = await self._llm.complete(
                    system=_SYSTEM_PROMPT,
                    user=user_prompt,
                )
            except TimeoutError as exc:
                last_failure = ("timeout", str(exc))
                _emit_failed_span(
                    reason="timeout",
                    raw_preview=str(exc)[:_RAW_PREVIEW_LIMIT],
                    retry_count=retry_count,
                )
                logger.warning(
                    "intent_router.failed reason=timeout attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue
            except IntentRouterEmptyResponse as exc:
                # SDK call succeeded but Haiku emitted no text — distinct
                # from transport failure and from unparseable text. Preserve
                # the diagnostic message (stop_reason, content blocks, usage)
                # in raw_preview so the GM panel can see why.
                last_failure = ("empty_response", str(exc))
                _emit_failed_span(
                    reason="empty_response",
                    raw_preview=str(exc)[:_RAW_PREVIEW_LIMIT],
                    retry_count=retry_count,
                )
                logger.warning(
                    "intent_router.failed reason=empty_response attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001 — transport boundary, see ADR-113 §5
                last_failure = ("transport", str(exc))
                _emit_failed_span(
                    reason="transport",
                    raw_preview=str(exc)[:_RAW_PREVIEW_LIMIT],
                    retry_count=retry_count,
                )
                logger.warning(
                    "intent_router.failed reason=transport attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue

            try:
                parsed = json.loads(raw_text)
            except (ValueError, TypeError) as exc:
                last_failure = ("unparseable", f"{type(exc).__name__}: {exc}")
                _emit_failed_span(
                    reason="unparseable",
                    raw_preview=(raw_text or "")[:_RAW_PREVIEW_LIMIT],
                    retry_count=retry_count,
                )
                logger.warning(
                    "intent_router.failed reason=unparseable attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue

            try:
                pkg = DispatchPackage.model_validate(parsed)
            except ValidationError as exc:
                last_failure = ("schema_invalid", type(exc).__name__)
                _emit_failed_span(
                    reason="schema_invalid",
                    raw_preview=(raw_text or "")[:_RAW_PREVIEW_LIMIT],
                    retry_count=retry_count,
                )
                logger.warning(
                    "intent_router.failed reason=schema_invalid attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue

            latency_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
            with intent_router_decompose_span(
                action_length=action_length,
                model=_INTENT_ROUTER_MODEL,
            ) as span:
                span.set_attribute("dispatch_count", _count_dispatches(pkg))
                span.set_attribute("latency_ms", int(latency_ms))
                span.set_attribute("retry_count", retry_count)
                span.set_attribute("confidence_global", float(pkg.confidence_global))
            return pkg

        assert last_failure is not None  # _MAX_TOTAL_ATTEMPTS >= 1
        reason, detail = last_failure
        raise IntentRouterFailure(f"intent_router producer failed after retry: {reason} ({detail})")


def _emit_failed_span(*, reason: str, raw_preview: str, retry_count: int) -> None:
    """Open and immediately close the ERROR-level failed span.

    The span captures one attempt's failure. Using the context manager
    inline keeps the call site terse — there is no in-attempt work to
    perform inside the span body.
    """
    with intent_router_failed_span(
        reason=reason,
        raw_preview=raw_preview,
        retry_count=retry_count,
    ):
        pass


__all__ = [
    "IntentRouter",
    "IntentRouterFailure",
    "IntentRouterLLM",
]
