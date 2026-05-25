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
    """Single-shot tool-use LLM contract the router consumes.

    Per ADR-102, structured output is produced through native tool-use:
    the adapter issues a forced ``tool_choice`` call and returns the
    ``tool_use`` block's already-structured ``input`` dict. There is no
    free-text JSON to parse — the ``unparseable`` failure mode is gone.
    Tests inject an ``AsyncMock`` returning a dict; the SDK-Haiku adapter
    in ``llm_factory.py`` is the live implementation.
    """

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


# ADR-102 tool-use contract: the router forces a single call to this tool
# whose input_schema IS the DispatchPackage schema, so Haiku returns
# structured input instead of fenced JSON.
_TOOL_NAME = "emit_dispatch_package"
_TOOL_DESCRIPTION = (
    "Emit the structured DispatchPackage for this single player action. "
    "Call this tool exactly once. Do not write prose."
)


def _dispatch_tool_schema() -> dict[str, Any]:
    """Build the tool input_schema from the DispatchPackage model.

    Reuses the same ``model_json_schema()`` path the tool registry uses
    (``tool_registry.py``) so the schema stays in lockstep with the model.
    """
    return DispatchPackage.model_json_schema()


_SYSTEM_PROMPT = """You are the Intent Router — an impartial structured-output reader.

Your job: read a player's action + the game state summary, then emit the
DispatchPackage by calling the ``emit_dispatch_package`` tool exactly once.
Never write prose. Put every field into the tool input.

For each player action:
  1. Resolve referents (pronouns, ellipses, demonstratives). Every resolution
     carries a confidence 0.0-1.0 and plausible alternatives. If nothing
     plausibly resolves, set resolved_to=null with confidence=0 — do NOT
     invent a filler.
  2. Emit subsystem dispatches keyed on the action's mechanical intent.
     Each dispatch carries a free-form ``params`` object. ``params`` is NOT a
     place to describe the action — it is the typed input the subsystem's
     handler reads. Emit exactly the keys listed; do not invent extra keys.
     Available subsystem keys and their required params:
       - confrontation: structured encounter (combat, negotiation, chase, etc.).
         params={"type": "<one of game_state.confrontation_types[].type>"}.
         Choose the single type whose category fits the action (a physical
         contest → a combat-category type; a parley → a social-category type;
         a flee/pursue → a movement-category type). The type MUST be one of the
         values listed in game_state.confrontation_types — never invent a type
         and never describe the action here instead of naming the type.
       - magic_working: spell or magical ability usage. params is a
         MagicWorking-shaped object (the spell/effect fields).
       - scenario_clue: clue/evidence discovery. params={"fact_id": "<id>"}
         (optional "summary", "category").
       - npc_agency: NPC reacts per role and disposition.
         params={"npc_name": "<name>"} (optional "situation").
       - distinctive_detail_hint: name a referent by its distinctive detail.
         params={"target": "<entity id>", "hint": "<detail>"}.
       - reflect_absence: player addresses someone/something not present.
  3. Emit narrator_instructions — must_narrate / must_not_narrate /
     distinctive_detail_for_referent / canonical_only_do_not_reveal_to_others.
  4. Set confidence_global to your overall confidence across the turn.

Every dispatch carries a visibility tag. Default visible_to="all" with empty
perception_fidelity unless the state clearly names asymmetric visibility.

Pydantic rejects unknown fields. Stay inside the schema. Emit everything
through the tool input — no preamble, no commentary, no extra text blocks."""


def _build_user_prompt(action: str, state_summary: Any) -> str:
    if isinstance(state_summary, str):
        state_text = state_summary
    else:
        state_text = json.dumps(state_summary, default=str, sort_keys=True)
    return (
        f"<game_state>\n{state_text}\n</game_state>\n"
        f"<raw_action>\n{action}\n</raw_action>\n"
        f"Call emit_dispatch_package once for this single action."
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
        tool_schema = _dispatch_tool_schema()
        action_length = len(action)
        start_ns = time.perf_counter_ns()
        last_failure: tuple[str, str] | None = None

        for attempt_index in range(_MAX_TOTAL_ATTEMPTS):
            retry_count = attempt_index  # 0 on first try, 1 on retry.
            try:
                tool_input = await self._llm.emit_tool(
                    system=_SYSTEM_PROMPT,
                    user=user_prompt,
                    tool_name=_TOOL_NAME,
                    tool_description=_TOOL_DESCRIPTION,
                    tool_schema=tool_schema,
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
                # SDK call succeeded but Haiku emitted no tool_use block —
                # distinct from transport failure and from a schema-invalid
                # tool input. Preserve the diagnostic message (stop_reason,
                # content blocks, usage) in raw_preview so the GM panel can
                # see why.
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
                pkg = DispatchPackage.model_validate(tool_input)
            except ValidationError as exc:
                last_failure = ("schema_invalid", type(exc).__name__)
                _emit_failed_span(
                    reason="schema_invalid",
                    raw_preview=str(tool_input)[:_RAW_PREVIEW_LIMIT],
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
