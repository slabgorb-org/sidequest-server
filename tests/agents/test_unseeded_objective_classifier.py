"""RED (Story 117-6, ADR-146 addendum): the un-seeded narrator-objective
classifier — detect open-ended objective-giving in narration WITHOUT a keyword
list.

THE PROBLEM these tests pin
---------------------------
Story 117-4 hardened the SEEDED path: when the Intent Router classified the turn
as accepting an offered objective (a ``quest_offer`` dispatch) but ``quest_log``
stayed empty, ``narration.unminted_objective.suspected`` fires — keyword-free,
riding the router. But the router is PRE-NARRATION and PLAYER-TURN-SCOPED, so it
has NO signal for an UN-SEEDED, narrator-INTRODUCED objective: a noir "discreet
job" hook the narrator improvises mid-scene with no ``pending_quest_offer`` and no
``quest_offer`` dispatch behind it. For that un-seeded case 117-4 retained the
brittle 13-phrase ``_UNMINTED_OBJECTIVE_MARKERS`` substring matcher as a backstop
— the Zork verb-set anti-pattern policing open-ended natural language. An
open-ended hook that trips zero curated phrases slips past, and the lie-detector
stays silent.

THE FIX 117-6 must build (the seam these tests force into existence)
-------------------------------------------------------------------
A POST-NARRATION classifier — a lightweight Haiku-via-SDK tool-use pass (the same
ADR-102 contract the Intent Router uses) that reads the narration text itself and
returns a binary "objective-given vs. not" decision with a confidence, regardless
of phrasing. It rides classification, not curated substrings.

These tests target the REAL seam: an injectable ``ObjectiveClassifierLLM`` (the
``emit_tool`` Protocol the router consumes) so the classifier is deterministic and
hits NO live API — mirroring tests/agents/test_intent_router.py. The fake drives
the decision, which is exactly what proves the classifier is classification-driven
rather than keyword-gated: the open-ended hook below trips ZERO curated markers,
yet the classifier reports objective-given because the LLM classified it so.

The module ``sidequest/agents/post_narration_classifier.py`` does not exist yet,
so every import below FAILS at collection-time per-test (import inside the test) —
RED for feature-absence, not an avoidable module-load error.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

# The exact perseus_cloud failure shape (session 594dcc7e, 2026-06-14): an
# open-ended noir job hook. It establishes a concrete objective (a giver hands the
# PC a task) but trips ZERO of the curated _UNMINTED_OBJECTIVE_MARKERS phrasings.
_OPEN_ENDED_HOOK = (
    'The floor-boss leans in, voice low. "I have a... situation. Someone of '
    "mine stopped checking in down in the under-levels. Discreet work. You look "
    'like the type who can handle that kind of thing."'
)

_QUIET_PROSE = (
    "You push through the bead curtain into the pot-house. The air is thick with "
    "smoke and the low murmur of off-shift dock hands. Someone is singing badly."
)


def _make_classifier_llm(response: dict[str, Any] | Exception) -> AsyncMock:
    """Mirror tests/agents/test_intent_router.py::_make_mock_router_llm.

    The classifier consumes ``emit_tool(...) -> dict`` (ADR-102 tool-use). When
    ``response`` is an Exception the mock raises it; otherwise it returns the dict.
    """
    mock = AsyncMock()
    if isinstance(response, Exception):
        mock.emit_tool = AsyncMock(side_effect=response)
    else:
        mock.emit_tool = AsyncMock(return_value=response)
    return mock


# ---------------------------------------------------------------------------
# Module + contract shape
# ---------------------------------------------------------------------------


def test_classifier_module_and_function_exist() -> None:
    """The new post-narration classifier module exposes an async
    ``classify_unseeded_objective`` — the seam 117-6 builds."""
    import asyncio

    from sidequest.agents.post_narration_classifier import classify_unseeded_objective

    assert asyncio.iscoroutinefunction(classify_unseeded_objective), (
        "classify_unseeded_objective must be async — a Haiku tool-use pass is I/O"
    )


def test_classification_dataclass_shape() -> None:
    """``UnseededObjectiveClassification`` carries the binary decision, a
    confidence in [0, 1], and optional reasoning for OTEL/debugging."""
    from sidequest.agents.post_narration_classifier import UnseededObjectiveClassification

    result = UnseededObjectiveClassification(
        is_objective_given=True, confidence=0.9, reasoning="giver named a task"
    )
    assert result.is_objective_given is True
    assert result.confidence == pytest.approx(0.9)
    assert result.reasoning == "giver named a task"


# ---------------------------------------------------------------------------
# Headline — classification, not keywords
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fires_on_open_ended_hook_via_classification() -> None:
    """The perseus_cloud repro. The open-ended hook trips ZERO curated markers,
    yet the classifier reports objective-given because the LLM classified it so —
    proving detection rides classification, not the keyword list."""
    from sidequest.agents.post_narration_classifier import classify_unseeded_objective

    from sidequest.agents.dispatch_engagement_watcher import _UNMINTED_OBJECTIVE_MARKERS

    # Premise guard: the hook genuinely trips none of the curated phrases, so the
    # OLD keyword path is silent on it. If a future marker covers it, fail loud so
    # the author picks a different hook (the whole point is keyword-free detection).
    lowered = _OPEN_ENDED_HOOK.lower()
    assert not any(m in lowered for m in _UNMINTED_OBJECTIVE_MARKERS), (
        "the open-ended hook must trip ZERO curated markers — that is the point of "
        "the classifier path"
    )

    llm = _make_classifier_llm(
        {"is_objective_given": True, "confidence": 0.88, "reasoning": "floor-boss hands a job"}
    )
    result = await classify_unseeded_objective(narration=_OPEN_ENDED_HOOK, llm=llm)

    assert result.is_objective_given is True, (
        "an open-ended objective hook the LLM classified as objective-giving must "
        "report is_objective_given=True even though zero curated markers match"
    )
    assert result.confidence == pytest.approx(0.88)
    assert llm.emit_tool.await_count == 1, "the classifier must consult the LLM exactly once"


@pytest.mark.asyncio
async def test_silent_on_quiet_prose() -> None:
    """Mood-setting prose with no objective: the LLM classifies it as not-objective
    and the classifier reports is_objective_given=False — no false positive."""
    from sidequest.agents.post_narration_classifier import classify_unseeded_objective

    llm = _make_classifier_llm(
        {"is_objective_given": False, "confidence": 0.95, "reasoning": "ambient scene only"}
    )
    result = await classify_unseeded_objective(narration=_QUIET_PROSE, llm=llm)

    assert result.is_objective_given is False
    assert llm.emit_tool.await_count == 1


@pytest.mark.asyncio
async def test_confidence_is_surfaced_from_llm() -> None:
    """The classifier surfaces the LLM's confidence verbatim so the GM panel can
    show how sure the detection was (not a hardcoded constant)."""
    from sidequest.agents.post_narration_classifier import classify_unseeded_objective

    llm = _make_classifier_llm(
        {"is_objective_given": True, "confidence": 0.42, "reasoning": "borderline hook"}
    )
    result = await classify_unseeded_objective(narration=_OPEN_ENDED_HOOK, llm=llm)

    assert result.confidence == pytest.approx(0.42), (
        "confidence must come from the classification, not a fixed default"
    )


# ---------------------------------------------------------------------------
# Cost guard (SOUL: Cost Scales with Drama) — do not spend a Haiku call on
# narration that cannot carry an un-seeded objective.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_narration_skips_llm_call() -> None:
    """Empty narration cannot establish an objective — the classifier must
    short-circuit WITHOUT spending a Haiku call (a quiet turn is cheap)."""
    from sidequest.agents.post_narration_classifier import classify_unseeded_objective

    llm = _make_classifier_llm(
        {"is_objective_given": True, "confidence": 1.0, "reasoning": "should never be read"}
    )
    result = await classify_unseeded_objective(narration="", llm=llm)

    assert result.is_objective_given is False
    assert llm.emit_tool.await_count == 0, (
        "empty narration must not cost a classification call (Cost Scales with Drama)"
    )
