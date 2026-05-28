"""Story 71-16: per-dispatch confidence scoring + threshold gating (ADR-113).

ADR-113 decided each mechanical dispatch engages its engine ONLY at or above a
confidence threshold (default 0.6, tunable per-subsystem in genre pack
``rules.yaml``); below threshold the dispatch degrades to a
``narrator_instruction`` hint rather than firing an engine. The router spine
shipped, but the confidence gate was never built — every dispatch currently
fires its engine unconditionally. This file is the RED-phase contract for the
gate.

AC coverage:
  AC1 — ``SubsystemDispatch`` carries a validated ``confidence: float``
        (0.0–1.0), populated by the ``IntentRouter`` for every dispatch.
  AC2 — In ``run_dispatch_bank``, a dispatch engages its engine only when
        ``confidence >= threshold``; below threshold it does NOT engage and
        instead produces a narrator hint (the ADR-113 degrade path).
  AC3 — Threshold is read per-subsystem from genre pack ``rules.yaml``
        (``RulesConfig.dispatch_confidence_thresholds``), defaulting to 0.6;
        a malformed (out-of-range) threshold fails loud.
  AC4 — Each dispatch emits an ``intent_router.subsystem`` span carrying its
        ``confidence``, the ``threshold`` applied, and the ``decision``
        (``engaged`` | ``degraded_to_hint``).
  AC5 — The live router→bank→engines ordering is unchanged; high-confidence
        dispatches behave exactly as today (all engage).

Contract notes (TEA-chosen, logged as deviations — Dev may rename with a
matching test update):
  - Threshold config field: ``RulesConfig.dispatch_confidence_thresholds:
    dict[str, float]`` (subsystem name → threshold), read by the bank from
    ``context["pack"].rules`` (``pack`` already flows into the bank context
    via ``intent_router_pass.execute_intent_router_pre_narrator_pass``).
  - Default threshold when a subsystem is unconfigured: 0.6 (ADR-113).
  - Gate decision is recorded on the existing ``intent_router.subsystem``
    span via new attributes ``confidence`` / ``threshold`` / ``decision``,
    and the span MUST fire for degraded (gated-out) dispatches too — not only
    engaged ones.

Project rule coverage:
  - "No Source-Text Wiring Tests" — gate behavior is proven by engine
    engagement (a recording probe subsystem records invocation) and by OTEL
    span attributes, never by grepping source. The one ``_SYSTEM_PROMPT``
    membership check is prompt-VOCABULARY (the accepted exception, mirrored
    from ``test_localdm_wiring``), not a wiring assertion.
  - "No Silent Fallbacks" — a malformed per-subsystem threshold must raise,
    never coerce to a silent default.
  - "OTEL Observability Principle" — every gate decision emits a span.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.agents.subsystems import (
    _REGISTRY as SUBSYSTEM_REGISTRY,
)
from sidequest.agents.subsystems import (
    SubsystemOutput,
    register_subsystem,
    run_dispatch_bank,
)
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

PROBE = "probe_gate"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


def _make_dispatch(
    subsystem: str,
    key: str,
    *,
    confidence: float,
    params: dict[str, Any] | None = None,
) -> SubsystemDispatch:
    """Build a dispatch carrying a per-dispatch ``confidence`` (AC1 field)."""
    return SubsystemDispatch(
        subsystem=subsystem,
        params=params or {},
        depends_on=[],
        idempotency_key=key,
        visibility=_tag_all(),
        confidence=confidence,
    )


def _make_package(dispatches: list[SubsystemDispatch]) -> DispatchPackage:
    return DispatchPackage(
        turn_id="t-71-16",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="(synthetic for 71-16 confidence-gate test)",
                resolved=[],
                dispatch=dispatches,
                lethality=[],
                narrator_instructions=[],
            )
        ],
        cross_player=[],
        confidence_global=1.0,
    )


@pytest.fixture
def probe_recorder():
    """Register a recording probe subsystem; yield the call-log list.

    The probe records each idempotency_key it is invoked with, so a test can
    assert the engine engaged (key present) or was gated out (key absent).
    Cleaned up from the live registry on teardown so it never leaks into other
    tests.
    """
    calls: list[str] = []

    async def _probe(dispatch: SubsystemDispatch, **_kwargs: Any) -> SubsystemOutput:
        calls.append(dispatch.idempotency_key)
        from sidequest.protocol.dispatch import NarratorDirective

        return SubsystemOutput(
            directives=[
                NarratorDirective(
                    kind="must_narrate",
                    payload=f"probe engaged {dispatch.idempotency_key}",
                    visibility=_tag_all(),
                )
            ],
            data={},
        )

    SUBSYSTEM_REGISTRY.pop(PROBE, None)
    register_subsystem(PROBE, _probe)
    try:
        yield calls
    finally:
        SUBSYSTEM_REGISTRY.pop(PROBE, None)


def _pack_with_thresholds(thresholds: dict[str, float]) -> SimpleNamespace:
    """A minimal stand-in for the GenrePack the bank reads thresholds from.

    The bank reads ``context["pack"].rules.dispatch_confidence_thresholds``.
    A real ``RulesConfig`` is constructed so the threshold contract (field
    name + validation) is exercised, wrapped in a namespace exposing ``.rules``
    the way a ``GenrePack`` does.
    """
    from sidequest.genre.models.rules import RulesConfig

    return SimpleNamespace(rules=RulesConfig(dispatch_confidence_thresholds=thresholds))


# ---------------------------------------------------------------------------
# AC1 — per-dispatch confidence field on SubsystemDispatch
# ---------------------------------------------------------------------------


def test_subsystem_dispatch_carries_confidence():
    """AC1: SubsystemDispatch accepts and preserves a confidence float."""
    d = _make_dispatch(PROBE, "k", confidence=0.73)
    assert d.confidence == pytest.approx(0.73)


def test_subsystem_dispatch_confidence_rejects_above_one():
    """AC1: confidence > 1.0 is invalid (mirrors Referent.confidence bounds)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _make_dispatch(PROBE, "k", confidence=1.5)


def test_subsystem_dispatch_confidence_rejects_below_zero():
    """AC1: confidence < 0.0 is invalid."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _make_dispatch(PROBE, "k", confidence=-0.01)


def test_subsystem_dispatch_confidence_is_required():
    """AC1: confidence has no silent default — every dispatch must carry it.

    No Silent Fallbacks: a defaulted confidence would let a router bug ship a
    0.0/1.0 score that silently engages or gates an engine. The field is
    required so an omission surfaces as a validation error (which routes the
    router to its retry/fail-loud path), never a quiet default.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SubsystemDispatch(
            subsystem=PROBE,
            params={},
            depends_on=[],
            idempotency_key="k",
            visibility=_tag_all(),
            # confidence intentionally omitted
        )


# ---------------------------------------------------------------------------
# AC1 — router populates per-dispatch confidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_round_trips_per_dispatch_confidence():
    """AC1: IntentRouter.decompose preserves the per-dispatch confidence the
    decomposition pass assigned (round-trips through DispatchPackage).

    A fake LLM returns a tool input with a per-dispatch confidence; the router
    must surface it on the resulting package rather than dropping it.
    """
    from sidequest.agents.intent_router import IntentRouter

    tool_input = {
        "turn_id": "t-router",
        "per_player": [
            {
                "player_id": "player:Alice",
                "raw_action": "I draw my blade on the bandit",
                "resolved": [],
                "dispatch": [
                    {
                        "subsystem": "confrontation",
                        "params": {"type": "duel"},
                        "depends_on": [],
                        "idempotency_key": "d1",
                        "visibility": {"visible_to": "all"},
                        "confidence": 0.42,
                    }
                ],
                "lethality": [],
                "narrator_instructions": [],
            }
        ],
        "cross_player": [],
        "confidence_global": 0.8,
    }

    class _FakeLLM:
        async def emit_tool(self, **_kwargs: Any) -> dict[str, Any]:
            return tool_input

    router = IntentRouter(llm=_FakeLLM())
    pkg = await router.decompose(action="I draw my blade on the bandit", state_summary={})

    assert pkg.per_player[0].dispatch[0].confidence == pytest.approx(0.42)


def test_router_prompt_instructs_per_dispatch_confidence():
    """AC1: the router system prompt tells Haiku to score each dispatch's
    confidence, so the decomposition pass populates the field.

    Prompt-vocabulary check (the accepted source-membership exception, same
    shape as test_localdm_wiring's vocabulary tests) — NOT a wiring assertion.
    """
    from sidequest.agents.intent_router import _SYSTEM_PROMPT

    assert "confidence" in _SYSTEM_PROMPT
    # The pre-existing prompt only mentions referent-resolution confidence and
    # confidence_global; per-dispatch confidence guidance must be added.
    lowered = _SYSTEM_PROMPT.lower()
    assert "dispatch" in lowered and "confidence" in lowered
    assert "per-dispatch confidence" in lowered or "confidence for each dispatch" in lowered


# ---------------------------------------------------------------------------
# AC2 — threshold gate: engage vs degrade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_threshold_dispatch_does_not_engage_engine(probe_recorder):
    """AC2: a dispatch below the (default 0.6) threshold does NOT engage its
    engine — the probe handler is never invoked."""
    pkg = _make_package([_make_dispatch(PROBE, "low", confidence=0.3)])
    res = await run_dispatch_bank(pkg)

    assert probe_recorder == [], (
        "below-threshold dispatch must NOT engage its engine; "
        f"probe was invoked: {probe_recorder}"
    )
    assert "low" not in res.outputs_by_key, (
        "gated-out dispatch must not appear in outputs_by_key (engine didn't run)"
    )


@pytest.mark.asyncio
async def test_below_threshold_dispatch_produces_narrator_hint(probe_recorder):
    """AC2: a gated-out dispatch degrades to a narrator hint — it is not
    silently dropped (the player's intent must still reach the narrator)."""
    pkg = _make_package([_make_dispatch(PROBE, "low", confidence=0.3)])
    res = await run_dispatch_bank(pkg)

    assert any(d.kind == "must_narrate" for d in res.directives), (
        "below-threshold dispatch must degrade to a narrator hint directive, "
        f"not vanish; directives were: {[d.kind for d in res.directives]}"
    )


@pytest.mark.asyncio
async def test_at_threshold_dispatch_engages_engine(probe_recorder):
    """AC2: confidence exactly AT the default threshold (0.6) engages — the
    gate is ``>=``, not ``>``."""
    pkg = _make_package([_make_dispatch(PROBE, "at", confidence=0.6)])
    res = await run_dispatch_bank(pkg)

    assert probe_recorder == ["at"], "confidence == threshold must engage (>= boundary)"
    assert "at" in res.outputs_by_key


@pytest.mark.asyncio
async def test_above_threshold_dispatch_engages_engine(probe_recorder):
    """AC2: a high-confidence dispatch engages its engine as today."""
    pkg = _make_package([_make_dispatch(PROBE, "high", confidence=0.95)])
    res = await run_dispatch_bank(pkg)

    assert probe_recorder == ["high"]
    assert "high" in res.outputs_by_key


@pytest.mark.asyncio
async def test_default_threshold_is_point_six(probe_recorder):
    """AC3: with no per-subsystem config, the gate uses the 0.6 default —
    0.59 is gated out, 0.60 engages."""
    pkg_just_below = _make_package([_make_dispatch(PROBE, "below", confidence=0.59)])
    res_below = await run_dispatch_bank(pkg_just_below)
    assert probe_recorder == [], "0.59 < default 0.6 must NOT engage"
    assert "below" not in res_below.outputs_by_key


# ---------------------------------------------------------------------------
# AC3 — per-subsystem tunable threshold (rules.yaml), fail-loud on malformed
# ---------------------------------------------------------------------------


def test_rules_config_accepts_per_subsystem_thresholds():
    """AC3: RulesConfig carries a per-subsystem confidence-threshold map."""
    from sidequest.genre.models.rules import RulesConfig

    cfg = RulesConfig(dispatch_confidence_thresholds={"confrontation": 0.8})
    assert cfg.dispatch_confidence_thresholds["confrontation"] == pytest.approx(0.8)


def test_rules_config_defaults_thresholds_empty():
    """AC3: unset threshold map defaults to empty (loader/bank applies 0.6)."""
    from sidequest.genre.models.rules import RulesConfig

    cfg = RulesConfig()
    assert cfg.dispatch_confidence_thresholds == {}


def test_rules_config_rejects_out_of_range_threshold():
    """AC3 / No Silent Fallbacks: a malformed (out-of-range) per-subsystem
    threshold must fail loud, not coerce to a silent default."""
    from pydantic import ValidationError

    from sidequest.genre.models.rules import RulesConfig

    with pytest.raises(ValidationError):
        RulesConfig(dispatch_confidence_thresholds={"confrontation": 1.5})


@pytest.mark.asyncio
async def test_per_subsystem_threshold_override_changes_gate(probe_recorder):
    """AC3: a pack rules.yaml override raises the bar for one subsystem — a
    0.7 dispatch that WOULD engage at the 0.6 default is gated out when the
    pack sets the probe's threshold to 0.9."""
    pack = _pack_with_thresholds({PROBE: 0.9})
    pkg = _make_package([_make_dispatch(PROBE, "mid", confidence=0.7)])
    res = await run_dispatch_bank(pkg, context={"pack": pack})

    assert probe_recorder == [], (
        "0.7 < per-subsystem override 0.9 must NOT engage even though it "
        "exceeds the 0.6 default"
    )
    assert "mid" not in res.outputs_by_key


@pytest.mark.asyncio
async def test_per_subsystem_threshold_override_lowering_engages(probe_recorder):
    """AC3: a pack can also LOWER the bar — a 0.4 dispatch engages when the
    pack sets the probe's threshold to 0.3 (below the 0.6 default)."""
    pack = _pack_with_thresholds({PROBE: 0.3})
    pkg = _make_package([_make_dispatch(PROBE, "lowbar", confidence=0.4)])
    res = await run_dispatch_bank(pkg, context={"pack": pack})

    assert probe_recorder == ["lowbar"], "0.4 >= override 0.3 must engage"
    assert "lowbar" in res.outputs_by_key


# ---------------------------------------------------------------------------
# AC4 — OTEL gate-decision span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_emits_span_decision_engaged(probe_recorder, otel_capture):
    """AC4: an engaged dispatch's intent_router.subsystem span carries its
    confidence, the threshold applied, and decision=engaged."""
    pkg = _make_package([_make_dispatch(PROBE, "eng", confidence=0.9)])
    await run_dispatch_bank(pkg)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "intent_router.subsystem"
        and dict(s.attributes or {}).get("subsystem") == PROBE
    ]
    assert len(spans) == 1, f"expected one probe subsystem span; got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("decision") == "engaged"
    assert attrs.get("confidence") == pytest.approx(0.9)
    assert attrs.get("threshold") == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_gate_emits_span_decision_degraded(probe_recorder, otel_capture):
    """AC4: a gated-out dispatch STILL emits an intent_router.subsystem span,
    with decision=degraded_to_hint — the GM panel must see the gate fire even
    when no engine engaged (the whole point of the lie detector)."""
    pkg = _make_package([_make_dispatch(PROBE, "deg", confidence=0.2)])
    await run_dispatch_bank(pkg)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "intent_router.subsystem"
        and dict(s.attributes or {}).get("subsystem") == PROBE
    ]
    assert len(spans) == 1, (
        f"a degraded dispatch must still emit its subsystem span; got {len(spans)}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("decision") == "degraded_to_hint"
    assert attrs.get("confidence") == pytest.approx(0.2)
    assert attrs.get("threshold") == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# AC5 — spine intact: high-confidence dispatches all engage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spine_intact_all_high_confidence_dispatches_engage(probe_recorder):
    """AC5: a turn of all-high-confidence dispatches behaves exactly as today —
    the gate does not drop any of them."""
    dispatches = [
        _make_dispatch(PROBE, f"k{i}", confidence=0.85) for i in range(3)
    ]
    pkg = _make_package(dispatches)
    res = await run_dispatch_bank(pkg)

    assert sorted(probe_recorder) == ["k0", "k1", "k2"], (
        "all high-confidence dispatches must engage; the gate must not drop "
        f"any of them. Engaged: {probe_recorder}"
    )
    for key in ("k0", "k1", "k2"):
        assert key in res.outputs_by_key


@pytest.mark.asyncio
async def test_mixed_confidence_turn_partitions_engage_and_degrade(probe_recorder, otel_capture):
    """AC2+AC4 integration: a turn mixing a high- and a low-confidence dispatch
    engages exactly the high one and degrades exactly the low one, each with the
    matching gate-decision span."""
    pkg = _make_package(
        [
            _make_dispatch(PROBE, "hi", confidence=0.9),
            _make_dispatch(PROBE, "lo", confidence=0.2),
        ]
    )
    res = await run_dispatch_bank(pkg)

    assert probe_recorder == ["hi"], "only the high-confidence dispatch engages"
    assert "hi" in res.outputs_by_key
    assert "lo" not in res.outputs_by_key

    decisions = {
        dict(s.attributes or {}).get("idempotency_key"): dict(s.attributes or {}).get("decision")
        for s in otel_capture.get_finished_spans()
        if s.name == "intent_router.subsystem"
        and dict(s.attributes or {}).get("subsystem") == PROBE
    }
    assert decisions == {"hi": "engaged", "lo": "degraded_to_hint"}
