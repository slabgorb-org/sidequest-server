"""Live wiring tests for the IntentRouter→dispatch-bank→narrator pipeline
introduced in Story 59-4 (ADR-113).

These tests pin the orchestrator-level cutover contract:

  AC2 — ``narration_apply.py`` no longer instantiates encounters from
    ``result.confrontation``. Behavioral test: set the field manually
    on a synthetic ``NarrationTurnResult``, run the apply path against
    a snapshot with no active encounter, assert NO encounter was
    created. The sidecar-driven creation site (lines 2528-2594 today)
    must be deleted in the cutover — leaving it in would re-enable the
    narrator-self-reports-engagement path the router was supposed to
    retire.

  AC5 — A pre-narrator pass helper exists and engages the confrontation
    engine before the narrator is invoked. The helper is the extraction
    surface Dev needs to add (Architect signpost: extract a callable
    like ``execute_intent_router_pre_narrator_pass`` so the call site in
    ``_execute_narration_turn`` is testable and the helper is reusable
    for future scenes/openings). The test stubs the IntentRouter and
    asserts the helper invokes the dispatch bank with the produced
    package against the supplied snapshot+pack, and that the encounter
    is created.

  AC5 wiring — The helper is wired into ``_execute_narration_turn``.
    Reflection-based assertion (CLAUDE.md "No Source-Text Wiring
    Tests"): import the session handler module and verify the helper
    symbol is reachable from it (either as a module-level import or as
    an attribute on a class). This is the legitimate
    reflection-on-runtime-types pattern, not source grep.

  AC7 — Fail-loud on router failure (memory ``feedback_no_fallbacks_hard``).
    Stub the router's LLM to always raise. Drive the helper. Assert:
    (a) the router's bounded retry happens (LLM stub called twice — the
        router's ``_MAX_TOTAL_ATTEMPTS = 2``),
    (b) ``IntentRouterFailure`` is raised by the helper (not swallowed
        into a "narrator-only fallback"),
    (c) the dispatch bank was NEVER called (no half-dispatch).

  Watcher coverage regression — The 59-3 ``run_dispatch_engagement_watcher``
    still fires its mismatch span when a router-dispatched confrontation
    fails to engage. Pinned by driving the watcher manually post-helper
    with a no-op handler stub. Cheap and proves the cross-component
    contract holds end-to-end on the live path.

These tests FAIL TODAY by design — the helper does not exist, the
narration_apply consumer is still live, and ``_execute_narration_turn``
is still dormant per the comment at lines 3172-3176.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return tracer, exporter


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _confrontation_package(*, enc_type: str = "negotiation") -> DispatchPackage:
    return DispatchPackage(
        turn_id="t-1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="I block his way and call the bluff.",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="confrontation",
                        params={"type": enc_type},
                        idempotency_key="k-conf-1",
                        visibility=_open_viz(),
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )


def _synthetic_pack_with_negotiation() -> Any:
    pytest.importorskip("sidequest.genre.models")
    from sidequest.genre.models import (  # type: ignore[import-not-found]
        ConfrontationDef,
        GenrePack,
        Rules,
    )

    cdef = ConfrontationDef(
        name="negotiation",
        category="social",
        description="A social negotiation.",
    )
    rules = Rules(confrontations=[cdef])
    return GenrePack(slug="test_pack", rules=rules)


def _snapshot_no_encounter() -> Any:
    from sidequest.game.session import GameSnapshot

    return GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        encounter=None,
        player_seats={"player:Alice": "Alice"},
    )


# ---------------------------------------------------------------------------
# AC2 — narration_apply no longer instantiates from result.confrontation
# ---------------------------------------------------------------------------


def test_narration_apply_ignores_result_confrontation_after_cutover() -> None:
    """AC2 (behavioral): set ``result.confrontation = 'negotiation'`` on a
    synthetic NarrationTurnResult, run the narration_apply path against a
    snapshot with no active encounter, and assert NO encounter is created.

    Behavioral, not source-grep (CLAUDE.md "No Source-Text Wiring Tests").
    The consumer block at ``narration_apply.py:2528-2594`` must be removed
    in the cutover — leaving it live would re-enable the
    narrator-self-reports-engagement path the router replaces. With AC3
    (begin_confrontation retired), nothing on the SDK path sets
    ``result.confrontation`` — but the consumer is the load-bearing
    guard: even if a stale codepath sets the field, the consumer being
    gone means the field cannot create an encounter.

    FAILS TODAY: the consumer block is live and would create the encounter.
    """
    pytest.importorskip("sidequest.server.narration_apply")
    from sidequest.protocol.narration import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    snap = _snapshot_no_encounter()
    pack = _synthetic_pack_with_negotiation()
    result = NarrationTurnResult(
        narration="The negotiation begins.",
        confrontation="negotiation",
        npcs_present=[],
    )

    # Try the canonical apply call. Signature may vary post-port — if Dev
    # finds the function takes additional args, update this test in the
    # same PR. Today the function is ``_apply_narration_result_to_snapshot``
    # at narration_apply.py:~2447.
    _apply_narration_result_to_snapshot(
        result=result,
        snapshot=snap,
        pack=pack,
        player_name="Alice",
    )

    assert snap.encounter is None, (
        "narration_apply must NOT create an encounter from result.confrontation "
        "post-cutover. The router-driven dispatch handler is the single creation "
        "path on the SDK backend; the sidecar consumer at narration_apply.py:2528 "
        f"must be removed. Got snapshot.encounter={snap.encounter!r}."
    )


# ---------------------------------------------------------------------------
# AC5 — pre-narrator router pass helper engages encounter before narrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_narrator_router_pass_helper_engages_confrontation() -> None:
    """AC5 (helper extraction): Story 59-4 extracts a pre-narrator router
    pass into a callable helper so the call site in
    ``_execute_narration_turn`` is testable in isolation. The helper:
      1. calls ``intent_router.decompose(action=, state_summary=)``,
      2. invokes ``run_dispatch_bank(package, context={...})`` with
         snapshot + pack + player_name kwargs,
      3. returns the produced DispatchPackage to the caller.

    By construction, if the helper returns successfully, encounter
    creation has already happened — the narrator (which runs after the
    helper) sees already-real state.

    Expected helper symbol (Dev confirms exact name during GREEN):
      ``sidequest.server.intent_router_pass.execute_intent_router_pre_narrator_pass``

    If Dev chooses a different module/name, update this import + log
    Design Deviation. The name MUST be a callable extracted from
    ``_execute_narration_turn``, not an inline block.

    FAILS TODAY: helper module does not exist.
    """
    try:
        from sidequest.server.intent_router_pass import (  # type: ignore[import-not-found]
            execute_intent_router_pre_narrator_pass,
        )
    except ImportError as exc:
        pytest.fail(
            "expected helper at "
            "sidequest.server.intent_router_pass."
            "execute_intent_router_pre_narrator_pass — extract the "
            "pre-narrator router pass from _execute_narration_turn "
            f"(websocket_session_handler.py:3172). ImportError: {exc}"
        )

    snap = _snapshot_no_encounter()
    pack = _synthetic_pack_with_negotiation()
    package = _confrontation_package()

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I block his way and call the bluff.",
        player_name="Alice",
    )

    router.decompose.assert_awaited_once()
    assert returned is package or returned == package, (
        "helper must return the DispatchPackage from the router so the "
        "downstream narrator prompt builder can consume it for redaction "
        "and narrator_instructions injection"
    )
    assert snap.encounter is not None, (
        "helper must have invoked the dispatch bank — the confrontation "
        "handler should have created the encounter on the snapshot before "
        "returning to the caller (which is pre-narrator)"
    )
    assert snap.encounter.encounter_type == "negotiation"


def test_pre_narrator_router_pass_helper_wired_into_session_handler() -> None:
    """AC5 (wiring): the helper must be imported by the
    ``websocket_session_handler`` module so the call site in
    ``_execute_narration_turn`` references it. Reflection-based: import
    the session handler module, inspect its module-level imports,
    confirm the helper symbol is present.

    CLAUDE.md "No Source-Text Wiring Tests" — this checks the module's
    runtime ``__dict__``, not source text. Equivalent to checking that
    the import statement landed without grepping the file.

    FAILS TODAY: ``websocket_session_handler`` has the dormant comment
    at lines 3172-3176 but no import or call.
    """
    import sidequest.server.websocket_session_handler as wsh

    # The helper must be reachable by name from the module's globals or
    # via a submodule import. Acceptable shapes:
    #   - direct import: from sidequest.server.intent_router_pass import ...
    #   - module import: import sidequest.server.intent_router_pass as ...
    #   - attribute on a module-level singleton
    candidates = {"execute_intent_router_pre_narrator_pass", "intent_router_pass"}
    found = candidates.intersection(set(dir(wsh)))
    assert found, (
        "websocket_session_handler must import the pre-narrator router pass "
        "helper or its module. Expected one of "
        f"{sorted(candidates)} in module globals; got nothing matching. "
        "If Dev wires the helper differently (e.g. as a method on a "
        "class), update this test AND log Design Deviation #5."
    )


# ---------------------------------------------------------------------------
# AC7 — fail-loud on router failure (no silent narrator-only continuation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_failure_surfaces_loud_after_bounded_retry() -> None:
    """AC7: when the router's LLM raises (timeout, transport error,
    unparseable, schema-invalid), the router itself retries ONCE
    (``intent_router.py:_MAX_TOTAL_ATTEMPTS = 2``), then raises
    ``IntentRouterFailure``. The pre-narrator helper MUST propagate the
    failure — NOT swallow it into a "fallback to narrator-only" branch.

    Memory ``feedback_no_fallbacks_hard``: a silent fallback here would
    let the player's action reach the narrator with zero mechanical
    backing, defeating the SOUL Illusionism counter the spine exists to
    deliver.

    FAILS TODAY: helper does not exist.
    """
    from sidequest.agents.intent_router import IntentRouter, IntentRouterFailure
    from sidequest.server.intent_router_pass import (  # type: ignore[import-not-found]
        execute_intent_router_pre_narrator_pass,
    )

    snap = _snapshot_no_encounter()
    pack = _synthetic_pack_with_negotiation()

    # Drive a REAL IntentRouter with a stubbed LLM that always raises.
    # This exercises the router's bounded-retry logic + failure-span
    # emission as a single coherent contract test (rather than mocking
    # them apart).
    stub_llm = MagicMock()
    stub_llm.complete = AsyncMock(side_effect=TimeoutError("synthetic"))
    router = IntentRouter(llm=stub_llm)

    with pytest.raises(IntentRouterFailure, match="timeout"):
        await execute_intent_router_pre_narrator_pass(
            intent_router=router,
            snapshot=snap,
            pack=pack,
            action="I do something.",
            player_name="Alice",
        )

    # Bounded retry: two attempts total. If the helper short-circuits or
    # silently retries beyond the router's contract, this assertion
    # catches it.
    assert stub_llm.complete.await_count == 2, (
        f"router contract: one initial attempt + one bounded retry = 2 "
        f"calls. Got {stub_llm.complete.await_count}. If the helper added "
        "its own retry layer, that violates the no-fallbacks rule."
    )

    # The bank was NEVER called — no half-dispatched state.
    assert snap.encounter is None, (
        "router failure must not result in a partially-engaged encounter — "
        "the bank should never have been invoked. Got "
        f"snapshot.encounter={snap.encounter!r}."
    )


# ---------------------------------------------------------------------------
# Watcher regression — 59-3 lie-detector still catches missed engagement
# on the new live path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_still_fires_when_new_handler_silently_no_ops() -> None:
    """Regression guard for the 59-3 watcher's coverage on the new live
    path. Drive the helper with a router that dispatches confrontation,
    but stub the handler to no-op (simulate a future regression where
    ``subsystems/confrontation.py`` silently breaks). The watcher,
    invoked with the consumed package and the post-helper snapshot, must
    fire ``dispatch_engagement.confrontation.mismatch``.

    This is the safety-net contract Story 59-3 promised: convincing
    prose with no mechanical backing is detectable. The cutover must
    keep that safety net wired on the new path.

    FAILS TODAY: helper does not exist; cannot drive the new path.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )
    from sidequest.server.intent_router_pass import (  # type: ignore[import-not-found]
        execute_intent_router_pre_narrator_pass,
    )

    snap = _snapshot_no_encounter()
    pack = _synthetic_pack_with_negotiation()
    package = _confrontation_package()

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    # Monkeypatch the confrontation handler to a no-op so we simulate a
    # handler regression. The watcher (which observes post-helper state)
    # should detect that the dispatched subsystem failed to engage.
    from sidequest.agents.subsystems import _REGISTRY, SubsystemOutput  # type: ignore[attr-defined]

    async def _no_op_handler(dispatch, **_kwargs):  # noqa: ARG001
        return SubsystemOutput()

    saved = _REGISTRY.get("confrontation")
    _REGISTRY["confrontation"] = _no_op_handler
    try:
        await execute_intent_router_pre_narrator_pass(
            intent_router=router,
            snapshot=snap,
            pack=pack,
            action="I block his way and call the bluff.",
            player_name="Alice",
        )
    finally:
        if saved is None:
            _REGISTRY.pop("confrontation", None)
        else:
            _REGISTRY["confrontation"] = saved

    # Encounter was NOT engaged (handler was no-op).
    assert snap.encounter is None

    # Watcher detects the mismatch.
    tracer, exporter = _fresh_tracer_and_exporter()
    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    mismatch_spans = [
        s for s in exporter.get_finished_spans()
        if s.name == "dispatch_engagement.confrontation.mismatch"
    ]
    assert len(mismatch_spans) == 1, (
        "watcher must fire dispatch_engagement.confrontation.mismatch when "
        "the router dispatched confrontation but no encounter exists post-"
        "dispatch. This is the 59-3 lie-detector contract — if it doesn't "
        "fire on the new live path, the GM panel is blind to handler "
        "regressions. Got span names: "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )
