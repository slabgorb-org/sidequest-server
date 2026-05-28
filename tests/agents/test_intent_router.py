"""Tests for IntentRouter — Story 59-2 RED phase.

The producer skeleton for the Epic 59 Intent Router spine (ADR-113). Revives
the dormant ``local_dm.py`` as ``intent_router.py`` (``IntentRouter`` class)
with a Haiku-via-SDK LlmClient adapter and a fail-loud retry policy.

Key contracts under test (story description + context-story-59-2.md):

* ``IntentRouter.decompose(action, state_summary) -> DispatchPackage`` returns
  a schema-valid package for synthetic input.
* ``DispatchPackage.degraded`` / ``degraded_reason`` are GONE from the
  protocol — the producer never returns a "degraded" package. On router
  failure, ONE bounded retry is attempted; if the retry also fails, the
  producer raises an explicit exception and emits an ERROR-level
  ``intent_router.failed`` OTEL span. No silent narrator-only continuation,
  no fallback DispatchPackage on failure paths
  (memory: ``feedback_no_fallbacks_hard``).
* The SDK-Haiku adapter mirrors the ``AsideResolver`` / ``_ASIDE_MODEL``
  pattern in ``llm_factory.py`` — single-shot Haiku call via the Anthropic
  SDK, routed through ``CallType.CLASSIFICATION``
  (``claude-haiku-4-5-20251001``).
* The legacy ``local_dm.decompose`` OTEL span retires; ``intent_router.decompose``
  (INFO) and ``intent_router.failed`` (ERROR) take its place. (CLAUDE.md
  OTEL Observability Principle — every subsystem decision is a span the GM
  panel can audit.)
* Wiring discipline (CLAUDE.md "Every Test Suite Needs a Wiring Test"):
  the wiring test lives in tests/agents/test_intent_router_wiring.py, with
  a 59-4 note that the LIVE pipeline call site lands then.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Fixtures — synthetic Haiku responses matching the post-cleanup
# DispatchPackage schema (NO ``degraded`` / ``degraded_reason`` fields).
# ---------------------------------------------------------------------------


@pytest.fixture
def haiku_response_pronoun_resolved() -> dict:
    """Synthetic SDK-Haiku tool input — a confrontation-shaped dispatch.

    Per ADR-102 the router consumes the ``tool_use`` block's structured
    ``input`` dict, so this fixture is a dict (not a JSON string).
    """
    return {
        "turn_id": "turn-010",
        "per_player": [
            {
                "player_id": "player:Alice",
                "raw_action": "Attack him!",
                "resolved": [
                    {
                        "token": "him",
                        "resolved_to": "npc:goblin_2",
                        "confidence": 0.55,
                        "alternatives": ["npc:goblin_1"],
                        "resolution_note": "most recent direct combatant",
                    }
                ],
                "dispatch": [
                    {
                        "subsystem": "distinctive_detail_hint",
                        "params": {
                            "target": "npc:goblin_2",
                            "hint": "broken tooth",
                        },
                        "depends_on": [],
                        "idempotency_key": "idem:turn-010:alice:0",
                        "confidence": 0.9,
                        "visibility": {
                            "visible_to": "all",
                            "perception_fidelity": {},
                            "secrets_for": [],
                            "redact_from_narrator_canonical": False,
                        },
                    }
                ],
                "lethality": [],
                "narrator_instructions": [
                    {
                        "kind": "distinctive_detail_for_referent",
                        "payload": "describe the goblin by its broken tooth",
                        "visibility": {
                            "visible_to": "all",
                            "perception_fidelity": {},
                            "secrets_for": [],
                            "redact_from_narrator_canonical": False,
                        },
                    }
                ],
            }
        ],
        "cross_player": [],
        "confidence_global": 0.55,
    }


@pytest.fixture
def haiku_response_quiet_turn() -> dict:
    """Quiet-turn dispatch — empty per_player + cross_player, valid schema."""
    return {
        "turn_id": "turn-quiet",
        "per_player": [],
        "cross_player": [],
        "confidence_global": 1.0,
    }


def _make_mock_router_llm(response: dict | Exception) -> AsyncMock:
    """Build a mocked router LLM adapter (ADR-102 tool-use shape).

    The router consumes ``emit_tool(...) -> dict`` — the ``tool_use``
    block's structured input. If the value is an exception instance,
    ``emit_tool`` raises it; otherwise it returns the dict.
    """
    mock = AsyncMock()
    if isinstance(response, BaseException):
        mock.emit_tool = AsyncMock(side_effect=response)
    else:
        mock.emit_tool = AsyncMock(return_value=response)
    return mock


def _make_sequenced_router_llm(*responses: dict | BaseException) -> AsyncMock:
    """Mock LLM that returns / raises a sequence across successive calls.

    Used for retry tests: first call may raise; second call may succeed
    (or fail again).
    """
    mock = AsyncMock()
    mock.emit_tool = AsyncMock(side_effect=list(responses))
    return mock


# ---------------------------------------------------------------------------
# AC-1: IntentRouter.decompose returns a schema-valid DispatchPackage on a
#       synthetic input fixture.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intent_router_decompose_returns_dispatch_package(
    haiku_response_pronoun_resolved: str,
) -> None:
    """AC-1: decompose returns a DispatchPackage on synthetic SDK-Haiku input.

    The fixture supplies a deterministic ``(action, state_summary)`` and a
    canned Haiku response; the router must parse it into a schema-valid
    ``DispatchPackage`` and the fields from the canned response must round-trip.
    """
    from sidequest.agents.intent_router import IntentRouter
    from sidequest.protocol.dispatch import DispatchPackage

    llm = _make_mock_router_llm(haiku_response_pronoun_resolved)
    router = IntentRouter(llm=llm)

    pkg = await router.decompose(
        action="Attack him!",
        state_summary={"scene": "goblins 1-3 and a bandit are in the room"},
    )

    assert isinstance(pkg, DispatchPackage), (
        "decompose must return DispatchPackage, not a degraded fallback shape"
    )
    assert pkg.turn_id == "turn-010"
    assert len(pkg.per_player) == 1
    referent = pkg.per_player[0].resolved[0]
    assert referent.resolved_to == "npc:goblin_2"
    assert pkg.confidence_global == pytest.approx(0.55)
    # The LLM was called exactly once on the happy path.
    assert llm.emit_tool.await_count == 1


@pytest.mark.asyncio
async def test_intent_router_forces_dispatch_package_tool(
    haiku_response_quiet_turn: dict,
) -> None:
    """ADR-102 wiring: decompose drives emit_tool with the DispatchPackage
    schema and the dispatch tool name — proving structured output comes
    from native tool-use, not free-text JSON that needs fence-stripping.
    """
    from sidequest.agents.intent_router import _TOOL_NAME, IntentRouter
    from sidequest.protocol.dispatch import DispatchPackage

    llm = _make_mock_router_llm(haiku_response_quiet_turn)
    router = IntentRouter(llm=llm)

    await router.decompose(action="wait", state_summary={})

    call_kwargs = llm.emit_tool.await_args.kwargs
    assert call_kwargs["tool_name"] == _TOOL_NAME
    assert call_kwargs["tool_schema"] == DispatchPackage.model_json_schema(), (
        "router must feed the DispatchPackage schema as the tool input_schema"
    )


@pytest.mark.asyncio
async def test_intent_router_prompt_documents_subsystem_params_contract(
    haiku_response_quiet_turn: dict,
) -> None:
    """Regression (playtest 2026-05-25): the router must TELL the model what
    ``params`` each subsystem expects, or Haiku fills the free-form ``params``
    dict with an ad-hoc semantic descriptor instead of the contract the
    dispatch handler reads.

    The live failure: a contested grapple routed correctly to
    ``subsystem=confrontation`` but emitted ``params={'action_type':
    'resistance_against_grapple', 'mechanic': 'strength_contest', ...}`` with
    NO ``type`` key. ``run_confrontation_dispatch`` requires
    ``params['type']`` (a ConfrontationDef type from the pack) and raised
    ValueError → zero engagement, Edge never ablated. The router already
    receives the valid ``confrontation_types`` enum in game_state (Story
    59-10); the prompt just never bound it to ``params['type']``.

    This asserts on the ``system`` prompt the router actually sends to the
    LLM (a behavioral observation of the producer, like
    ``test_intent_router_forces_dispatch_package_tool`` asserts on the sent
    ``tool_name``) — NOT a source-text grep of production files.
    """
    from sidequest.agents.intent_router import IntentRouter

    llm = _make_mock_router_llm(haiku_response_quiet_turn)
    router = IntentRouter(llm=llm)

    await router.decompose(action="I grab him and wrench him out of the pool", state_summary={})

    system = llm.emit_tool.await_args.kwargs["system"]
    # The confrontation params contract MUST be communicated, bound to the
    # closed enum already supplied in game_state.confrontation_types.
    assert "confrontation_types" in system, (
        "router prompt must point the model at the confrontation_types enum "
        "for choosing the confrontation params['type']"
    )
    assert 'params={"type"' in system, (
        "router prompt must document that a confrontation dispatch's params "
        "carry the chosen confrontation type as params['type'] — without this "
        'the handler raises ValueError("missing required params[\'type\']")'
    )


@pytest.mark.asyncio
async def test_intent_router_decompose_quiet_turn_empty_dispatch(
    haiku_response_quiet_turn: str,
) -> None:
    """AC-1 edge case: empty per_player / cross_player is schema-valid."""
    from sidequest.agents.intent_router import IntentRouter

    llm = _make_mock_router_llm(haiku_response_quiet_turn)
    router = IntentRouter(llm=llm)

    pkg = await router.decompose(
        action="I look around quietly.",
        state_summary={"scene": "empty tavern"},
    )

    assert pkg.per_player == []
    assert pkg.cross_player == []


# ---------------------------------------------------------------------------
# AC-4: Module docstring rewritten — DORMANT header REMOVED.
# ---------------------------------------------------------------------------


def test_intent_router_module_docstring_is_production_not_dormant() -> None:
    """AC-4: intent_router.py module docstring describes a production
    producer per ADR-113, not the historical DORMANT shelving.

    Per CLAUDE.md "No Source-Text Wiring Tests" we DON'T grep production
    source files; we check the module's runtime ``__doc__`` attribute,
    which is a runtime-type interrogation (the documented legitimate
    exception)."""
    import sidequest.agents.intent_router as intent_router_module

    doc = intent_router_module.__doc__ or ""
    # The dormant shelving framing must be gone.
    assert "DORMANT" not in doc, (
        "intent_router.py docstring must not declare the module DORMANT "
        "(ADR-113 restored the live path)"
    )
    # The ADR-113 framing must be present.
    assert "ADR-113" in doc, (
        "intent_router.py docstring must reference ADR-113 as the "
        "architectural authority for the restored live path"
    )


# ---------------------------------------------------------------------------
# AC-5: Fail-loud failure path — ERROR span, ONE bounded retry, explicit
#       surface; absence of silent narrator-only continuation (memory rule
#       feedback_no_fallbacks_hard).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intent_router_fail_loud_on_timeout(otel_capture) -> None:
    """AC-5 (timeout): TimeoutError on both attempts → raises, two ERROR spans."""
    from sidequest.agents.intent_router import IntentRouter, IntentRouterFailure

    llm = _make_sequenced_router_llm(
        TimeoutError("haiku slow"),
        TimeoutError("haiku slow"),
    )
    router = IntentRouter(llm=llm)

    with pytest.raises(IntentRouterFailure):
        await router.decompose(
            action="x",
            state_summary={"scene": "y"},
        )

    # ONE bounded retry — the LLM was called exactly twice.
    assert llm.emit_tool.await_count == 2, (
        "fail-loud retry policy must attempt exactly one retry; "
        f"got {llm.emit_tool.await_count} total attempts"
    )

    failed_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.failed"
    ]
    assert len(failed_spans) == 2, (
        f"expected two intent_router.failed ERROR spans (attempt + retry); got {len(failed_spans)}"
    )
    attrs = dict(failed_spans[0].attributes or {})
    assert "timeout" in str(attrs.get("reason", "")).lower(), (
        f"intent_router.failed span must record reason; attrs={attrs}"
    )


@pytest.mark.asyncio
async def test_intent_router_fail_loud_on_transport_error(otel_capture) -> None:
    """AC-5 (transport): Anthropic SDK exception on both attempts → raises."""
    from sidequest.agents.intent_router import IntentRouter, IntentRouterFailure

    # Use a generic RuntimeError to stand in for any Anthropic SDK error
    # (anthropic.APIError / APIConnectionError / RateLimitError etc.) so
    # the test does not import the anthropic SDK directly.
    llm = _make_sequenced_router_llm(
        RuntimeError("anthropic.APIConnectionError: refused"),
        RuntimeError("anthropic.APIConnectionError: refused"),
    )
    router = IntentRouter(llm=llm)

    with pytest.raises(IntentRouterFailure):
        await router.decompose(action="x", state_summary={})

    assert llm.emit_tool.await_count == 2
    failed_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.failed"
    ]
    assert len(failed_spans) == 2


@pytest.mark.asyncio
async def test_intent_router_fail_loud_on_empty_response(otel_capture) -> None:
    """No ``tool_use`` block on both attempts → IntentRouterFailure with
    ``empty_response`` reason.

    Under the ADR-102 tool-use contract there is no free-text JSON to
    parse — the ``unparseable`` failure mode is gone. The remaining
    "model produced nothing usable" mode is the adapter raising
    ``IntentRouterEmptyResponse`` when the forced ``tool_choice`` response
    carries no ``tool_use`` block (refusal, pause-turn, all-text blocks).
    The producer must categorize this as its own failure mode so the GM
    panel and operator logs can see it distinctly.
    """
    from sidequest.agents.intent_router import IntentRouter, IntentRouterFailure
    from sidequest.agents.llm_factory import IntentRouterEmptyResponse

    diagnostic = IntentRouterEmptyResponse(
        "Haiku returned no tool_use block (stop_reason='refusal', blocks=[], usage=None)"
    )
    llm = _make_sequenced_router_llm(diagnostic, diagnostic)
    router = IntentRouter(llm=llm)

    with pytest.raises(IntentRouterFailure) as excinfo:
        await router.decompose(action="x", state_summary={})

    assert llm.emit_tool.await_count == 2
    assert "empty_response" in str(excinfo.value), (
        f"failure message must categorize as empty_response; got {excinfo.value!s}"
    )
    failed_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.failed"
    ]
    assert len(failed_spans) == 2
    reasons = {dict(s.attributes or {}).get("reason") for s in failed_spans}
    assert reasons == {"empty_response"}, (
        f"both attempts must record reason=empty_response; got reasons={reasons}"
    )
    preview = str(dict(failed_spans[0].attributes or {}).get("raw_preview", ""))
    assert "stop_reason" in preview, (
        f"empty_response raw_preview must carry the diagnostic stop_reason; got preview={preview!r}"
    )


@pytest.mark.asyncio
async def test_intent_router_fail_loud_on_schema_invalid_output(otel_capture) -> None:
    """AC-5 (schema-invalid): tool input that fails DispatchPackage pydantic
    validation on both attempts → raises.

    Even with forced tool-use, the model can emit a structurally-typed
    input that violates a model constraint; the router treats that as a
    producer failure, not a degraded path.
    """
    from sidequest.agents.intent_router import IntentRouter, IntentRouterFailure

    schema_invalid = {
        # confidence_global is required; omitting it triggers a
        # pydantic ValidationError. (Also stands in for any other
        # schema violation Haiku might emit.)
        "turn_id": "t-bad",
        "per_player": [],
        "cross_player": [],
    }
    llm = _make_sequenced_router_llm(schema_invalid, schema_invalid)
    router = IntentRouter(llm=llm)

    with pytest.raises(IntentRouterFailure):
        await router.decompose(action="x", state_summary={})

    assert llm.emit_tool.await_count == 2
    failed_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.failed"
    ]
    assert len(failed_spans) == 2


@pytest.mark.asyncio
async def test_intent_router_retry_succeeds_does_not_raise(
    haiku_response_quiet_turn: str,
    otel_capture,
) -> None:
    """AC-5 (retry success path): first attempt fails, second succeeds → no raise.

    The success ``intent_router.decompose`` span records ``retry_count=1``;
    an ``intent_router.failed`` span fired for the first attempt is also
    present so the GM panel sees the failure even though the turn recovered.
    """
    from sidequest.agents.intent_router import IntentRouter

    llm = _make_sequenced_router_llm(
        TimeoutError("transient"),
        haiku_response_quiet_turn,
    )
    router = IntentRouter(llm=llm)

    pkg = await router.decompose(action="x", state_summary={})

    assert pkg.per_player == []
    assert llm.emit_tool.await_count == 2

    spans = otel_capture.get_finished_spans()
    decompose_spans = [s for s in spans if s.name == "intent_router.decompose"]
    assert len(decompose_spans) == 1, (
        f"exactly one success span must fire when retry succeeds; got {len(decompose_spans)}"
    )
    attrs = dict(decompose_spans[0].attributes or {})
    assert attrs.get("retry_count") == 1, (
        f"intent_router.decompose must record retry_count=1 on the "
        f"retry-success path; got attrs={attrs}"
    )

    failed_spans = [s for s in spans if s.name == "intent_router.failed"]
    assert len(failed_spans) == 1, (
        "the first-attempt failure must still emit an ERROR span (GM "
        "panel sees the contract violation even though we recovered)"
    )


@pytest.mark.asyncio
async def test_intent_router_no_silent_fallback_on_retry_fail() -> None:
    """AC-5 (the lie-detector): on retry-also-fails, the router does NOT
    return a "degraded" DispatchPackage — it RAISES.

    Memory rule ``feedback_no_fallbacks_hard``: no silent narrator-only
    continuation, no degraded shape, no returning an empty package on
    failure. Failure surfaces as an exception so the orchestrator can
    decide what to do (explicit surface, not implicit swallow).
    """
    from sidequest.agents.intent_router import IntentRouter, IntentRouterFailure

    llm = _make_sequenced_router_llm(
        TimeoutError("first"),
        TimeoutError("retry"),
    )
    router = IntentRouter(llm=llm)

    # Capturing into a variable to prove no DispatchPackage is returned.
    captured_return = None
    with pytest.raises(IntentRouterFailure):
        captured_return = await router.decompose(action="x", state_summary={})

    assert captured_return is None, (
        "decompose must raise on retry-also-fails — NEVER return a "
        "DispatchPackage (degraded or otherwise). Returning a package on "
        "failure violates feedback_no_fallbacks_hard."
    )


# ---------------------------------------------------------------------------
# AC-6: OTEL spans — intent_router.decompose (INFO) + intent_router.failed
#       (ERROR); legacy local_dm.decompose retired.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intent_router_emits_decompose_span_with_required_attrs(
    haiku_response_quiet_turn: str,
    otel_capture,
) -> None:
    """AC-6: success path emits ``intent_router.decompose`` (INFO) carrying
    action_length, model, dispatch_count, latency_ms, retry_count, and
    (when present in the LLM output) confidence_global."""
    from sidequest.agents.intent_router import IntentRouter

    llm = _make_mock_router_llm(haiku_response_quiet_turn)
    router = IntentRouter(llm=llm)

    action = "I look around quietly."
    await router.decompose(action=action, state_summary={"scene": "x"})

    spans = otel_capture.get_finished_spans()
    decompose_spans = [s for s in spans if s.name == "intent_router.decompose"]
    assert len(decompose_spans) == 1, (
        f"exactly one intent_router.decompose span must fire on success; got {len(decompose_spans)}"
    )
    attrs = dict(decompose_spans[0].attributes or {})
    # Required attribute coverage. (Latency value will be small but must
    # be present and numeric.)
    for required_key in (
        "action_length",
        "model",
        "dispatch_count",
        "latency_ms",
        "retry_count",
        "confidence_global",
    ):
        assert required_key in attrs, (
            f"intent_router.decompose missing required attr {required_key!r}; attrs={attrs}"
        )
    assert attrs["action_length"] == len(action)
    assert attrs["retry_count"] == 0, "happy path must record retry_count=0"
    assert attrs["dispatch_count"] == 0, (
        f"quiet-turn fixture has no dispatches; got dispatch_count={attrs['dispatch_count']}"
    )
    assert "claude-haiku-4-5" in str(attrs["model"]), (
        f"intent_router.decompose must record the Haiku model id; got model={attrs['model']!r}"
    )


@pytest.mark.asyncio
async def test_intent_router_failed_span_is_error_level(otel_capture) -> None:
    """AC-5/AC-6: the ``intent_router.failed`` span is ERROR-level so
    OTEL backends and the GM panel surface it as a real failure (not an
    INFO breadcrumb)."""
    from opentelemetry.trace import StatusCode

    from sidequest.agents.intent_router import IntentRouter, IntentRouterFailure

    llm = _make_sequenced_router_llm(
        TimeoutError("x"),
        TimeoutError("y"),
    )
    router = IntentRouter(llm=llm)

    with pytest.raises(IntentRouterFailure):
        await router.decompose(action="x", state_summary={})

    failed_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.failed"
    ]
    assert failed_spans, "intent_router.failed must fire on producer failure"
    # Each failed span carries OTEL StatusCode.ERROR (or sets the
    # ``error`` attribute) so the GM panel surfaces it as a real failure.
    for span in failed_spans:
        status = span.status
        attrs = dict(span.attributes or {})
        is_error_status = status.status_code == StatusCode.ERROR
        is_error_attr = bool(attrs.get("error"))
        assert is_error_status or is_error_attr, (
            f"intent_router.failed must carry ERROR status or error=True; "
            f"got status={status} attrs={attrs}"
        )


def test_legacy_local_dm_decompose_span_constant_retired() -> None:
    """AC-6: ``local_dm.decompose`` span name retires; no ``local_dm.*``
    span constants survive on the SDK path.

    Per CLAUDE.md "No Source-Text Wiring Tests" we interrogate the
    telemetry package's runtime symbols, not source text. ``local_dm.py``
    being importable as a module is fine (rename in progress) — but the
    legacy span name constants ``SPAN_LOCAL_DM_*`` must not survive in the
    public ``sidequest.telemetry.spans`` namespace, or the GM panel will
    still route to the dead names.
    """
    from sidequest.telemetry import spans as spans_pkg

    # No legacy ``SPAN_LOCAL_DM_*`` constants survive at the package level.
    leftover = [name for name in dir(spans_pkg) if name.startswith("SPAN_LOCAL_DM")]
    assert leftover == [], (
        "legacy SPAN_LOCAL_DM_* constants must be retired (renamed or "
        f"deleted); still present in sidequest.telemetry.spans: {leftover}"
    )


# ---------------------------------------------------------------------------
# Sanity probe: the public IntentRouter exception type is named per
# story scope (used by AC-5 failure-path tests above).
# ---------------------------------------------------------------------------


def test_intent_router_failure_exception_importable() -> None:
    """Sanity: ``IntentRouterFailure`` is importable from the router
    module so callers in 59-4 can pattern-match the explicit failure
    surface."""
    from sidequest.agents.intent_router import IntentRouterFailure

    # It is an Exception subclass — not a sentinel value, not a NamedTuple.
    assert issubclass(IntentRouterFailure, Exception), (
        "IntentRouterFailure must be an Exception subclass so the explicit "
        "fail-loud surface can be caught by the orchestrator (59-4)"
    )
