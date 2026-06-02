"""Pre-narrator precondition gate — drops structurally-inert dispatches before
the dispatch bank AND before the 59-3 lie-detector watcher (Story 59-8, ADR-113).

Playtest 59-8 (Glenross, tea_and_murder) surfaced a guaranteed false-feeling
``dispatch_engagement.scenario_clue.mismatch`` on every investigative turn:
the Intent Router routes "I search the room" to ``scenario_clue``, but Glenross
ships no ADR-053 scenario graph, so ``snapshot.scenario_state is None``.
``consume_clue_footnotes`` then no-ops and the watcher reports a mismatch it
can NEVER avoid — re-running the dispatch changes nothing.

That mismatch is a TRUE positive (zero mechanical backing) but an UNAVOIDABLE
one in a no-scenario world, and it blocks 59-8 AC3 ("zero
``dispatch_engagement.*.mismatch``"). The fix is a PRECONDITION GATE, not
silencing the watcher: when a subsystem's world-level precondition is
structurally unmet, drop its dispatch from the package before it runs and
before the watcher reads it, emitting a loud ``intent_router.dispatch.gated``
OTEL span per drop (NOT a silent fallback — CLAUDE.md "No Silent Fallbacks").

Critically, the gate fires ONLY when the precondition is structurally unmet.
In a real ADR-053 scenario world (``scenario_state`` present) the scenario_clue
dispatch passes through untouched and the watcher's genuine-mismatch detection
is unaffected — pinned by ``test_*_in_scenario_world_*`` below.

Module under test (created by Dev in GREEN):
- ``sidequest/agents/dispatch_precondition_gate.py``

Project rule coverage (CLAUDE.md / SOUL):
- "Every Test Suite Needs a Wiring Test" —
  ``test_router_pass_gates_scenario_clue_so_watcher_sees_no_mismatch`` drives
  the real ``execute_intent_router_pre_narrator_pass`` and asserts the watcher
  goes quiet end-to-end.
- "No Source-Text Wiring Tests" — wiring proven by behavior (the watcher span
  count), never by grepping source.
- "OTEL Observability Principle" — every gate decision emits a span; a clean
  turn emits zero.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.political_state import PoliticalState
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.premises import (
    BlocAwakening,
    BlocDef,
    PremiseClaim,
    PremiseCollapse,
    PremiseDef,
    PremiseDrain,
)
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# OTEL plumbing — isolated tracer/exporter per test
# ---------------------------------------------------------------------------


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return tracer, exporter


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _dispatch(
    *,
    subsystem: str,
    params: dict[str, Any],
    idempotency_key: str,
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem=subsystem,
        params=params,
        idempotency_key=idempotency_key,
        confidence=1.0,
        visibility=_open_viz(),
    )


def _scenario_clue_dispatch(
    *, fact_id: str = "the_visitor_at_the_halt", key: str = "k-clue-1"
) -> SubsystemDispatch:
    return _dispatch(
        subsystem="scenario_clue",
        params={"fact_id": fact_id, "summary": f"Clue {fact_id}", "category": "Lore"},
        idempotency_key=key,
    )


def _package_with(*dispatches: SubsystemDispatch, turn_id: str = "turn-1") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="I search the room for clues.",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _snapshot(*, scenario_state: ScenarioState | None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="glenross",
        scenario_state=scenario_state,
        player_seats={"player:Alice": "Alice"},
    )


def _all_dispatch_subsystems(package: DispatchPackage) -> list[str]:
    out: list[str] = []
    for pd in package.per_player:
        out.extend(d.subsystem for d in pd.dispatch)
    for ca in package.cross_player:
        out.extend(d.subsystem for d in ca.dispatch)
    return out


# --- witnessed_act fixtures (mirror the scenario_clue builders above) -------
# witnessed_act keys off snapshot.political_state (the wry_whimsy premise/bloc
# layer) the way scenario_clue keys off snapshot.scenario_state. A hydrated
# snapshot needs a PoliticalState built from a world carrying premises + blocs.


def _oz_political_state() -> PoliticalState:
    """Minimal hydrated PoliticalState (the Oz premise/bloc layer), matching the
    fixture in tests/agents/test_witnessed_act_subsystem.py."""
    humbug = PremiseDef(
        premise_id="humbug",
        authority="the_wizard",
        claim=PremiseClaim(subject="the_wizard", proposition="great and terrible"),
        belief_reserve=90,
        propped_by=["munchkins"],
        drained_by=[PremiseDrain(act="expose", belief_delta=40)],
        collapse=PremiseCollapse(threshold=20, outcome="He flees."),
    )
    munchkins = BlocDef(
        bloc_id="munchkins",
        defiance=5,
        grants_belief_to=["humbug"],
        awakening_acts=[BlocAwakening(act="rally", defiance_delta=10)],
        tipping_threshold=70,
        tipped_outcome="Revolt.",
    )
    world = SimpleNamespace(premises=[humbug], blocs=[munchkins])
    state = PoliticalState.from_world(world)
    assert state is not None  # a world with premises+blocs always hydrates
    return state


def _political_snapshot(*, political_state: PoliticalState | None) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        player_seats={"player:Alice": "Alice"},
    )
    snap.political_state = political_state
    return snap


def _witnessed_act_dispatch(*, key: str = "k-wa-1") -> SubsystemDispatch:
    return _dispatch(
        subsystem="witnessed_act",
        params={"act_id": "expose", "witnesses": ["Dorothy"]},
        idempotency_key=key,
    )


# ===========================================================================
# Pure function — gate_inert_dispatches(package, snapshot)
# ===========================================================================


def test_scenario_clue_dropped_when_scenario_state_none() -> None:
    """No scenario graph → scenario_clue is structurally inert → dropped, and
    a GatedDispatch records the reason for the span layer."""
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    package = _package_with(_scenario_clue_dispatch())
    snap = _snapshot(scenario_state=None)

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == [], (
        "scenario_clue must be removed from the package when scenario_state is "
        f"None; got {_all_dispatch_subsystems(filtered)}"
    )
    assert len(gated) == 1
    assert gated[0].subsystem == "scenario_clue"
    assert gated[0].idempotency_key == "k-clue-1"
    assert "scenario_state is None" in gated[0].reason


def test_scenario_clue_kept_when_scenario_state_present() -> None:
    """A real ADR-053 scenario world → scenario_clue passes through untouched,
    nothing gated. The watcher's genuine-mismatch detection stays intact."""
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    package = _package_with(_scenario_clue_dispatch())
    snap = _snapshot(scenario_state=ScenarioState())

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == ["scenario_clue"]
    assert gated == []


def test_non_scenario_dispatch_kept_even_when_scenario_state_none() -> None:
    """Only scenario_clue keys off scenario_state. A confrontation dispatch
    must pass through even in a no-scenario world."""
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    package = _package_with(
        _dispatch(subsystem="confrontation", params={"type": "social_duel"}, idempotency_key="k-c")
    )
    snap = _snapshot(scenario_state=None)

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == ["confrontation"]
    assert gated == []


def test_scenario_clue_dropped_but_sibling_dispatch_preserved() -> None:
    """Selective filtering: a turn that dispatches scenario_clue AND npc_agency
    drops only the inert scenario_clue; the npc_agency dispatch survives the
    package rebuild."""
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    package = _package_with(
        _scenario_clue_dispatch(key="k-clue-1"),
        _dispatch(subsystem="npc_agency", params={"npc_name": "Sir Iain"}, idempotency_key="k-npc"),
    )
    snap = _snapshot(scenario_state=None)

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == ["npc_agency"]
    assert [g.subsystem for g in gated] == ["scenario_clue"]


# ===========================================================================
# OTEL wrapper — run_dispatch_precondition_gate emits one span per drop
# ===========================================================================


def test_gate_emits_one_gated_span_per_drop() -> None:
    """Each dropped dispatch emits a loud intent_router.dispatch.gated span so
    the GM panel sees the skip — not a silent fallback."""
    from sidequest.agents.dispatch_precondition_gate import run_dispatch_precondition_gate

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_scenario_clue_dispatch())
    snap = _snapshot(scenario_state=None)

    filtered = run_dispatch_precondition_gate(package=package, snapshot=snap, tracer=tracer)

    assert _all_dispatch_subsystems(filtered) == []
    spans = [s for s in exporter.get_finished_spans() if s.name == "intent_router.dispatch.gated"]
    assert len(spans) == 1, (
        f"expected exactly 1 gated span, got {[s.name for s in exporter.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("subsystem") == "scenario_clue"
    assert "scenario_state is None" in str(attrs.get("reason", ""))


def test_gate_emits_no_span_when_nothing_gated() -> None:
    """Quiet turn: scenario world present → zero gated spans (no false skip)."""
    from sidequest.agents.dispatch_precondition_gate import run_dispatch_precondition_gate

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_scenario_clue_dispatch())
    snap = _snapshot(scenario_state=ScenarioState())

    run_dispatch_precondition_gate(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if s.name == "intent_router.dispatch.gated"]
    assert spans == []


# ===========================================================================
# witnessed_act precondition (Story 59-29 — co-located from
# test_witnessed_act_subsystem.py so the gate's coverage lives in one place).
#
# witnessed_act is structurally inert when snapshot.political_state is None
# (the world ships no wry_whimsy premise/bloc layer), exactly as scenario_clue
# is inert when scenario_state is None. These mirror the scenario_clue blocks
# above, keying off political_state instead.
# ===========================================================================


def test_witnessed_act_dropped_when_political_state_none() -> None:
    """No premise/bloc layer → witnessed_act is structurally inert → dropped,
    and a GatedDispatch records the reason for the span layer."""
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    package = _package_with(_witnessed_act_dispatch())
    snap = _political_snapshot(political_state=None)

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == [], (
        "witnessed_act must be removed from the package when political_state is "
        f"None; got {_all_dispatch_subsystems(filtered)}"
    )
    assert len(gated) == 1
    assert gated[0].subsystem == "witnessed_act"
    assert gated[0].idempotency_key == "k-wa-1"
    assert "political_state is None" in gated[0].reason


def test_witnessed_act_kept_when_political_state_present() -> None:
    """A hydrated premise/bloc world → witnessed_act passes through untouched,
    nothing gated. The watcher's genuine-mismatch detection stays intact."""
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    package = _package_with(_witnessed_act_dispatch())
    snap = _political_snapshot(political_state=_oz_political_state())

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == ["witnessed_act"]
    assert gated == []


def test_witnessed_act_dropped_but_sibling_dispatch_preserved() -> None:
    """Selective filtering: a turn that dispatches witnessed_act AND npc_agency
    into a no-political-state world drops only the inert witnessed_act; the
    npc_agency dispatch (no precondition) survives the package rebuild."""
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    package = _package_with(
        _witnessed_act_dispatch(key="k-wa-1"),
        _dispatch(subsystem="npc_agency", params={"npc_name": "Dorothy"}, idempotency_key="k-npc"),
    )
    snap = _political_snapshot(political_state=None)

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == ["npc_agency"]
    assert [g.subsystem for g in gated] == ["witnessed_act"]


def test_gate_emits_one_witnessed_act_gated_span_per_drop() -> None:
    """Each dropped witnessed_act dispatch emits a loud
    intent_router.dispatch.gated span so the GM panel sees the skip."""
    from sidequest.agents.dispatch_precondition_gate import run_dispatch_precondition_gate

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_witnessed_act_dispatch())
    snap = _political_snapshot(political_state=None)

    filtered = run_dispatch_precondition_gate(package=package, snapshot=snap, tracer=tracer)

    assert _all_dispatch_subsystems(filtered) == []
    spans = [s for s in exporter.get_finished_spans() if s.name == "intent_router.dispatch.gated"]
    assert len(spans) == 1, (
        f"expected exactly 1 gated span, got {[s.name for s in exporter.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("subsystem") == "witnessed_act"
    assert "political_state is None" in str(attrs.get("reason", ""))


def test_gate_emits_no_span_for_witnessed_act_when_political_state_present() -> None:
    """Quiet turn: premise/bloc world present → zero gated spans (no false skip)."""
    from sidequest.agents.dispatch_precondition_gate import run_dispatch_precondition_gate

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_witnessed_act_dispatch())
    snap = _political_snapshot(political_state=_oz_political_state())

    run_dispatch_precondition_gate(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if s.name == "intent_router.dispatch.gated"]
    assert spans == []


# ===========================================================================
# Wiring / end-to-end — the gate is live in the pre-narrator router pass and
# makes the 59-3 watcher go quiet for the no-scenario case (AC3).
# ===========================================================================


def _synthetic_pack() -> Any:
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig()
    return pack


@pytest.mark.asyncio
async def test_router_pass_gates_scenario_clue_so_watcher_sees_no_mismatch() -> None:
    """AC3 end-to-end + wiring: drive the real pre-narrator router pass with a
    router that dispatches scenario_clue into a no-scenario snapshot. The
    returned package must carry NO scenario_clue dispatch, and the 59-3 watcher
    run on that returned package must emit ZERO
    dispatch_engagement.scenario_clue.mismatch spans.

    This is the exact Glenross failure (playtest 59-8 turn 2) and its fix: the
    gate removes the guaranteed-inert dispatch before the watcher ever sees it.
    """
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = _snapshot(scenario_state=None)
    pack = _synthetic_pack()
    package = _package_with(_scenario_clue_dispatch())

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, _bank = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I search the room for clues.",
        player_name="Alice",
    )

    assert _all_dispatch_subsystems(returned) == [], (
        "the pre-narrator pass must gate the inert scenario_clue dispatch out of "
        "the package it returns (the same object turn_context.dispatch_package "
        f"the watcher reads); got {_all_dispatch_subsystems(returned)}"
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    run_dispatch_engagement_watcher(package=returned, snapshot=snap, tracer=tracer)

    mismatches = [
        s
        for s in exporter.get_finished_spans()
        if s.name == "dispatch_engagement.scenario_clue.mismatch"
    ]
    assert mismatches == [], (
        "AC3: with the gate live, the no-scenario world must produce ZERO "
        "scenario_clue mismatch spans. Got "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )


@pytest.mark.asyncio
async def test_router_pass_does_not_gate_scenario_clue_in_scenario_world() -> None:
    """Regression guard: in a real scenario world the gate MUST NOT fire, so a
    genuinely-unengaged scenario_clue dispatch still trips the 59-3 watcher.
    Proves the gate narrows the false-positive without blinding the lie-detector.
    """
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    # scenario_state present but the dispatched fact never enters discovered_clues
    # (empty graph) → genuine mismatch the watcher should still catch.
    snap = _snapshot(scenario_state=ScenarioState())
    pack = _synthetic_pack()
    package = _package_with(_scenario_clue_dispatch(fact_id="unknowable_fact"))

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, _bank = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I search the room for clues.",
        player_name="Alice",
    )

    assert _all_dispatch_subsystems(returned) == ["scenario_clue"], (
        "scenario_clue must pass through untouched in a scenario world"
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    run_dispatch_engagement_watcher(package=returned, snapshot=snap, tracer=tracer)

    mismatches = [
        s
        for s in exporter.get_finished_spans()
        if s.name == "dispatch_engagement.scenario_clue.mismatch"
    ]
    assert len(mismatches) == 1, (
        "the gate must NOT fire in a scenario world; the watcher must still "
        "catch a genuinely-unengaged scenario_clue dispatch. Got "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )
