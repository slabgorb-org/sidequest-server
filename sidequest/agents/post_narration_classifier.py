"""Story 117-6 (ADR-146 addendum): the un-seeded narrator-objective classifier.

The post-narration counterpart to the Intent Router's pre-narration pass. When the
narrator IMPROVISES an open-ended objective hook mid-scene — a noir "discreet job",
an NPC who hands the player a task — with no structured ``quest_offer`` behind it,
the pre-narration router has no signal for it (it is player-turn-scoped, and there
is no ``pending_quest_offer`` to detect acceptance of). Story 117-4 hardened that
SEEDED path; for the UN-SEEDED case it left the brittle 13-phrase
``_UNMINTED_OBJECTIVE_MARKERS`` substring matcher as a backstop — the Zork verb-set
anti-pattern policing open-ended natural language (SOUL: The Zork Problem).

This module replaces that backstop with a real classification: a single-shot Haiku
tool-use pass (ADR-102) that reads the narration prose itself and decides whether an
objective was given, regardless of phrasing.

**Cost-gated (SOUL: Cost Scales with Drama).** The watcher spends a Haiku call ONLY
when the turn could carry an un-seeded objective: a non-empty narration, an EMPTY
``quest_log`` (nothing minted — the same gate the sync detector uses), and NO router
``quest_offer`` dispatch (the seeded 117-4 path already owns that). A quiet,
already-minted, or router-seeded turn costs nothing.

On a hit the watcher emits ``narration.unminted_objective.suspected`` with
``detection_method="classifier"`` so the GM panel (the lie-detector) can tell a real
classification from a keyword-backstop guess (OTEL Observability Principle). Like its
sync sibling ``run_unminted_objective_watcher`` it is **non-fatal by contract** — a
post-narration observability pass never tears down turn delivery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from opentelemetry import trace

from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.telemetry.spans.dispatch_engagement import (
    dispatch_engagement_watcher_crashed_span,
    narration_unminted_objective_span,
)

logger = logging.getLogger(__name__)

# Cost guard (SOUL: Cost Scales with Drama): bound the narration before the SDK call
# so a runaway-long narration (context overflow, a verbose genre) cannot grind
# unbounded input-token cost on the classification. The objective signal is in the
# opening prose, not the ten-thousandth char. Mirrors infer_archetype_from_freeform's
# 4,000-char fodder cap. Truncation is logged LOUD, never silent (No Silent Fallbacks).
_MAX_NARRATION_CHARS = 4_000

_TOOL_NAME = "report_unseeded_objective"
_TOOL_DESCRIPTION = (
    "Report whether the narration just GAVE the player an open-ended objective — a "
    "task, job, errand, or goal a character or NPC hands the player, or a hook the "
    "narrator plants as something concrete to pursue. Call this tool exactly once."
)
_SYSTEM = (
    "You judge whether a passage of tabletop-RPG narration introduces an OBJECTIVE "
    "for the player: someone hands them a job, names a task, or the scene plants a "
    "concrete thing to go do. Mood, scenery, ambient dialogue, and combat blows are "
    "NOT objectives. Decide from the prose itself regardless of phrasing — do not "
    "rely on specific keywords. Call the tool with your judgement and a confidence."
)


def _tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "is_objective_given": {
                "type": "boolean",
                "description": "True if the narration gave the player an open-ended objective.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in the judgement, 0.0 to 1.0.",
            },
            "reasoning": {
                "type": "string",
                "description": "One short phrase naming the objective, or why there is none.",
            },
        },
        "required": ["is_objective_given", "confidence"],
        "additionalProperties": False,
    }


class ObjectiveClassifierLLM(Protocol):
    """Single-shot tool-use LLM contract (ADR-102), the same shape the Intent
    Router consumes. Tests inject an ``AsyncMock`` returning a dict; the SDK-Haiku
    adapter ``build_unseeded_objective_classifier_llm`` in ``llm_factory.py`` is the
    live implementation."""

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class UnseededObjectiveClassification:
    """The classifier's verdict on a single narration passage."""

    is_objective_given: bool
    confidence: float
    reasoning: str | None = None


async def classify_unseeded_objective(
    *,
    narration: str,
    llm: ObjectiveClassifierLLM,
) -> UnseededObjectiveClassification:
    """Classify whether ``narration`` gave the player an open-ended objective.

    A single forced tool call (ADR-102): the decision rides classification of the
    prose, not a curated keyword list. Empty/blank narration short-circuits with no
    SDK spend (Cost Scales with Drama).
    """
    if not narration.strip():
        return UnseededObjectiveClassification(
            is_objective_given=False, confidence=1.0, reasoning="empty narration"
        )
    if len(narration) > _MAX_NARRATION_CHARS:
        logger.warning(
            "unseeded_objective_classifier narration truncated from %d to %d chars",
            len(narration),
            _MAX_NARRATION_CHARS,
        )
        narration = narration[:_MAX_NARRATION_CHARS]
    # ADR-047 defense-in-depth: the narration rides inside a structural delimiter so
    # instruction-shaped prose reads as quoted material. The forced tool_choice is
    # the actual boundary; this just hardens the prompt shape.
    user = (
        "Narration to judge — does it give the player an objective?\n"
        f"<narration>\n{narration}\n</narration>"
    )
    result = await llm.emit_tool(
        system=_SYSTEM,
        user=user,
        tool_name=_TOOL_NAME,
        tool_description=_TOOL_DESCRIPTION,
        tool_schema=_tool_schema(),
    )
    return UnseededObjectiveClassification(
        is_objective_given=bool(result.get("is_objective_given", False)),
        confidence=float(result.get("confidence", 0.0)),
        reasoning=result.get("reasoning"),
    )


def _package_has_quest_offer(package: DispatchPackage | None) -> bool:
    """True when the router emitted ANY ``quest_offer`` dispatch this turn.

    Such a turn is router-SEEDED for quests — the sync 117-4 path
    (``detect_unminted_objective``) already owns it (accept-without-mint beeps,
    decline stays silent). The un-seeded classifier exists for the router-SILENT
    case, so it defers whenever a quest_offer signal is present.
    """
    if package is None:
        return False
    for pd in package.per_player:
        if any(d.subsystem == "quest_offer" for d in pd.dispatch):
            return True
    for ca in package.cross_player:
        if any(d.subsystem == "quest_offer" for d in ca.dispatch):
            return True
    return False


async def run_unseeded_objective_classifier_watcher(
    *,
    narration: str,
    snapshot: GameSnapshot,
    llm: ObjectiveClassifierLLM,
    package: DispatchPackage | None = None,
    tracer: trace.Tracer | None = None,
) -> None:
    """Run the un-seeded objective classifier and emit one classifier-tagged span
    on a hit.

    Cost gates (no Haiku call when the turn cannot carry an un-seeded objective):
    blank narration, a non-empty ``quest_log`` (already minted), or a router
    ``quest_offer`` this turn (owned by the seeded path). **Non-fatal by contract**
    — identical discipline to :func:`run_unminted_objective_watcher`: any exception
    is caught and surfaced as the watcher-crashed span rather than tearing down turn
    delivery.
    """
    try:
        if not narration.strip():
            return
        quest_log = getattr(snapshot, "quest_log", None) or {}
        if quest_log:
            return  # already minted — the empty-quest_log gate stands the pass down
        if _package_has_quest_offer(package):
            return  # router-seeded — the sync 117-4 path owns this turn

        classification = await classify_unseeded_objective(narration=narration, llm=llm)
        if not classification.is_objective_given:
            return

        evidence = (
            "narrator introduced an open-ended objective with no router quest_offer "
            "and no minted quest "
            f"(confidence={classification.confidence:.2f}"
            + (f": {classification.reasoning}" if classification.reasoning else "")
            + ") — classified post-narration, keyword-free"
        )
        with narration_unminted_objective_span(
            evidence=evidence,
            detection_method="classifier",
            _tracer=tracer,
        ):
            pass
    except Exception as exc:  # noqa: BLE001 — observability must never abort the turn
        logger.error(
            "unseeded_objective_classifier.crashed error_type=%s error=%s "
            "(turn pipeline continues; un-seeded classification lost this turn)",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        with dispatch_engagement_watcher_crashed_span(
            error_type=type(exc).__name__,
            error=str(exc),
            _tracer=tracer,
        ):
            pass


__all__ = [
    "ObjectiveClassifierLLM",
    "UnseededObjectiveClassification",
    "classify_unseeded_objective",
    "run_unseeded_objective_classifier_watcher",
]
