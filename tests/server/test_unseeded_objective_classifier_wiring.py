"""RED (Story 117-6, AC-3 / AC-5): wire the un-seeded objective classifier into
the live post-narration pipeline, gated for cost, emitting the GM-panel span on
an open-ended hook — and keep the keyword matcher only as a retained backstop.

This is the integration counterpart to tests/agents/test_unseeded_objective_classifier.py
(which pins the pure classifier). Here we pin:

  * the async watcher ``run_unseeded_objective_classifier_watcher`` that drives the
    classifier and emits ``narration.unminted_objective.suspected`` with
    detection_method='classifier' on a hit;
  * its cost gates (SOUL: Cost Scales with Drama) — it must NOT spend a Haiku call
    when the narration cannot carry an un-seeded objective: empty quest_log is the
    necessary condition, and the router-SEEDED case is already owned by the sync
    117-4 path, so a turn carrying a ``quest_offer`` dispatch must short-circuit;
  * production wiring — the live WS turn pipeline (``_execute_narration_turn``,
    an async method) AWAITS the watcher (AST inspection, not source-grep);
  * AC-3 — the legacy ``_UNMINTED_OBJECTIVE_MARKERS`` keyword matcher is RETAINED
    as a backstop (not deleted), so router-silent edge cases still have coverage.

The module/function do not exist yet, so the behavioral tests FAIL on import and
the wiring tests FAIL on the missing call — RED for feature-absence.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

_OPEN_ENDED_HOOK = (
    'The floor-boss leans in, voice low. "I have a... situation. Someone of '
    "mine stopped checking in down in the under-levels. Discreet work. You look "
    'like the type who can handle that kind of thing."'
)

_SPAN = "narration.unminted_objective.suspected"


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _snapshot(*, quest_log: dict[str, QuestEntry] | None = None) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    if quest_log is not None:
        snap.quest_log = quest_log
    return snap


def _make_classifier_llm(response: dict[str, Any]) -> AsyncMock:
    mock = AsyncMock()
    mock.emit_tool = AsyncMock(return_value=response)
    return mock


def _objective_given_llm() -> AsyncMock:
    return _make_classifier_llm(
        {"is_objective_given": True, "confidence": 0.87, "reasoning": "floor-boss hands a job"}
    )


def _quest_offer_package() -> DispatchPackage:
    """A turn the router classified as a quest_offer accept — the SEEDED case the
    sync 117-4 watcher already owns. The async classifier must defer to it."""
    dispatch = SubsystemDispatch(
        subsystem="quest_offer",
        params={"quest_id": "floor_boss_missing_person", "decision": "accept"},
        idempotency_key="qo-1",
        confidence=0.9,
        visibility=VisibilityTag(visible_to="all"),
    )
    return DispatchPackage(
        turn_id="turn-1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="yeah, alright",
                dispatch=[dispatch],
            )
        ],
        confidence_global=0.9,
    )


# ---------------------------------------------------------------------------
# Async watcher — emits the classifier-tagged span on a hit
# ---------------------------------------------------------------------------


def test_classifier_watcher_is_async() -> None:
    from sidequest.agents.post_narration_classifier import (
        run_unseeded_objective_classifier_watcher,
    )

    assert asyncio.iscoroutinefunction(run_unseeded_objective_classifier_watcher), (
        "the un-seeded classifier watcher must be async — it awaits a Haiku pass; "
        "the enclosing handler method _execute_narration_turn is already async"
    )


async def test_watcher_emits_classifier_span_on_open_ended_hook() -> None:
    """The headline: an open-ended hook, empty quest_log, no router quest_offer →
    the classifier fires and the watcher emits the span tagged 'classifier'."""
    from sidequest.agents.post_narration_classifier import (
        run_unseeded_objective_classifier_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    llm = _objective_given_llm()
    await run_unseeded_objective_classifier_watcher(
        narration=_OPEN_ENDED_HOOK,
        snapshot=_snapshot(quest_log={}),
        llm=llm,
        package=None,
        tracer=tracer,
    )

    spans = [s for s in exporter.get_finished_spans() if s.name == _SPAN]
    assert spans, "expected the unminted-objective span on the classifier path; got none"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("detection_method") == "classifier", (
        f"the classifier-path span must be tagged detection_method='classifier'; got {attrs}"
    )
    assert llm.emit_tool.await_count == 1


async def test_watcher_silent_when_classifier_says_no_objective() -> None:
    from sidequest.agents.post_narration_classifier import (
        run_unseeded_objective_classifier_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    llm = _make_classifier_llm(
        {"is_objective_given": False, "confidence": 0.9, "reasoning": "ambient only"}
    )
    await run_unseeded_objective_classifier_watcher(
        narration=_OPEN_ENDED_HOOK,
        snapshot=_snapshot(quest_log={}),
        llm=llm,
        package=None,
        tracer=tracer,
    )

    assert not [s for s in exporter.get_finished_spans() if s.name == _SPAN], (
        "a not-objective classification must not beep"
    )


# ---------------------------------------------------------------------------
# Cost gates (SOUL: Cost Scales with Drama) — no Haiku call when it cannot pay off
# ---------------------------------------------------------------------------


async def test_watcher_skips_llm_when_quest_already_minted() -> None:
    """A non-empty quest_log means the objective was tracked — the empty-quest_log
    gate (shared with the sync detector) stands the classifier down BEFORE spending
    a Haiku call."""
    from sidequest.agents.post_narration_classifier import (
        run_unseeded_objective_classifier_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    llm = _objective_given_llm()
    minted = {
        "q": QuestEntry(title="The Floor-Boss's Missing Person", objective="o", status="active")
    }
    await run_unseeded_objective_classifier_watcher(
        narration=_OPEN_ENDED_HOOK,
        snapshot=_snapshot(quest_log=minted),
        llm=llm,
        package=None,
        tracer=tracer,
    )

    assert llm.emit_tool.await_count == 0, (
        "a minted quest must short-circuit the classifier without a Haiku call"
    )
    assert not [s for s in exporter.get_finished_spans() if s.name == _SPAN]


async def test_watcher_defers_to_seeded_router_path() -> None:
    """When the router emitted a quest_offer dispatch this turn, the SEEDED 117-4
    path already owns the detection. The un-seeded classifier must defer (no Haiku
    call, no double-beep) — it exists only for the router-SILENT case."""
    from sidequest.agents.post_narration_classifier import (
        run_unseeded_objective_classifier_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    llm = _objective_given_llm()
    await run_unseeded_objective_classifier_watcher(
        narration=_OPEN_ENDED_HOOK,
        snapshot=_snapshot(quest_log={}),
        llm=llm,
        package=_quest_offer_package(),
        tracer=tracer,
    )

    assert llm.emit_tool.await_count == 0, (
        "a router-seeded turn (quest_offer present) is owned by the sync 117-4 path "
        "— the un-seeded classifier must not also spend a Haiku call"
    )


async def test_watcher_skips_llm_on_empty_narration() -> None:
    from sidequest.agents.post_narration_classifier import (
        run_unseeded_objective_classifier_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    llm = _objective_given_llm()
    await run_unseeded_objective_classifier_watcher(
        narration="",
        snapshot=_snapshot(quest_log={}),
        llm=llm,
        package=None,
        tracer=tracer,
    )

    assert llm.emit_tool.await_count == 0


# ---------------------------------------------------------------------------
# Non-fatal contract — observability must never tear down the turn
# ---------------------------------------------------------------------------


async def test_watcher_swallows_classifier_failure() -> None:
    """Identical discipline to run_unminted_objective_watcher: a classifier
    exception is caught (the turn keeps delivering), never re-raised — AND the crash
    is SURFACED as the watcher-crashed span, not silently dropped (OTEL Observability
    Principle: a swallowed error with no telemetry blinds the GM panel)."""
    from sidequest.agents.post_narration_classifier import (
        run_unseeded_objective_classifier_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    llm = AsyncMock()
    llm.emit_tool = AsyncMock(side_effect=RuntimeError("haiku 500"))

    # Must NOT raise — a post-narration observability pass is non-fatal by contract.
    await run_unseeded_objective_classifier_watcher(
        narration=_OPEN_ENDED_HOOK,
        snapshot=_snapshot(quest_log={}),
        llm=llm,
        package=None,
        tracer=tracer,
    )

    crashed = [
        s for s in exporter.get_finished_spans() if s.name == "dispatch_engagement.watcher.crashed"
    ]
    assert crashed, (
        "a swallowed classifier exception must emit the watcher-crashed span so the "
        "GM panel shows the lie-detector broke this turn — not a silent drop"
    )
    attrs = dict(crashed[0].attributes or {})
    assert attrs.get("error_type") == "RuntimeError", (
        f"the crashed span must carry the error_type; got {attrs}"
    )
    # And NO false objective span on the failure path.
    assert not [s for s in exporter.get_finished_spans() if s.name == _SPAN]


# ---------------------------------------------------------------------------
# AC-3 — keyword matcher RETAINED as a backstop (not deleted)
# ---------------------------------------------------------------------------


def test_keyword_backstop_symbol_retained() -> None:
    """117-6 supersedes the keyword matcher with the classifier but must NOT delete
    it — it stays as the emergency backstop for the router-silent / classifier-
    unavailable edge case (story AC-3)."""
    from sidequest.agents import dispatch_engagement_watcher as mod

    assert hasattr(mod, "_UNMINTED_OBJECTIVE_MARKERS"), (
        "_UNMINTED_OBJECTIVE_MARKERS must be retained as a backstop, not deleted"
    )
    assert len(mod._UNMINTED_OBJECTIVE_MARKERS) > 0


# ---------------------------------------------------------------------------
# AC-5 — production wiring: the live async handler awaits the classifier watcher
# ---------------------------------------------------------------------------


def test_classifier_watcher_imported_into_session_handler() -> None:
    """The handler module's runtime namespace must reference the classifier watcher
    (reflection, not source-grep) — else it can never fire in production."""
    import sys

    import sidequest.server.session_handler  # noqa: F401 — load-order fix

    handler_mod = sys.modules["sidequest.server.websocket_session_handler"]
    has_function = "run_unseeded_objective_classifier_watcher" in handler_mod.__dict__
    has_module = "post_narration_classifier" in handler_mod.__dict__
    assert has_function or has_module, (
        "websocket_session_handler must import the un-seeded objective classifier "
        "watcher — without it the classifier lie-detector cannot fire in production"
    )


def test_live_handler_awaits_classifier_watcher() -> None:
    """Production wiring: the WS turn pipeline must CALL
    run_unseeded_objective_classifier_watcher, and because the watcher is async the
    call must be awaited. AST inspection of the call + its await context (CLAUDE.md
    "No Source-Text Wiring Tests")."""
    import ast
    import inspect

    import sidequest.server.websocket_session_handler as handler_mod

    tree = ast.parse(inspect.getsource(handler_mod))

    awaited_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name:
            awaited_calls.append(name)

    assert "run_unseeded_objective_classifier_watcher" in awaited_calls, (
        "the live handler must `await run_unseeded_objective_classifier_watcher(...)` "
        "in the post-narration block so the un-seeded classifier fires in production "
        "— found no awaited call to it"
    )
