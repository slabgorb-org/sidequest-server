"""Tests for the router-vs-engine lie-detector watcher (Story 59-3).

The watcher repurposes the confrontation_intent_validator subsystem from
"narrator-action_rewrite vs engaged-encounter mismatch" (the 59-1 reprompt
trigger) to "router-dispatched-subsystem vs engine-engaged-on-snapshot
mismatch" — generalized across the full dispatch vocabulary (confrontation,
magic_working, scenario_clue) per ADR-113 and the epic-59 reframe.

The watcher is a pure function ``(package, post_turn_snapshot) → list[DispatchMismatch]``
running post-turn. It does NOT correct, retry, or re-dispatch — it observes
and emits an OTEL span per mismatch so the GM panel (Sebastien's
mechanics-first surface, Keith's lie-detector) sees router-dispatched-but-
engine-didn't-engage turns immediately.

Module under test (created by Dev in GREEN):
- ``sidequest/agents/dispatch_engagement_watcher.py``

Engagement witnesses pinned during RED (story context AC4 deferred this to
Dev/TEA during RED; resolved here):

- confrontation: ``snapshot.encounter is not None`` AND
  ``snapshot.encounter.encounter_type == params["type"]``
- magic_working: a ``WorkingRecord`` in ``snapshot.magic_state.working_log``
  matches ``params["actor"]``
- scenario_clue: ``params["fact_id"] in snapshot.scenario_state.discovered_clues``

See the TEA Assessment in ``.session/59-3-session.md`` for the engagement-
witness deviation note (TEA pinned the witnesses Architect flagged as
ambiguous during context creation).

Project rule coverage (CLAUDE.md / SOUL):
- "Every Test Suite Needs a Wiring Test" — ``test_watcher_wired_into_session_handler``
- "No Source-Text Wiring Tests" — wiring tests use reflection
  (``module.__dict__``) and behavior assertions, never source-grep
- "No Silent Fallbacks" — a malformed dispatch (missing required param) is
  surfaced as a loud mismatch span, NOT a silent no-op and NOT an uncaught
  crash that would take down post-narration WS turn-delivery
  (``test_watcher_surfaces_malformed_confrontation_dispatch_as_mismatch_not_crash``)
- "OTEL Observability Principle" — every mismatch path emits a span;
  every happy path emits ZERO spans (no false positive on quiet turns)
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot
from sidequest.magic.models import (
    HardLimit,
    LedgerBarSpec,
    WorldKnowledge,
    WorldMagicConfig,
)
from sidequest.magic.state import MagicState, WorkingRecord
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# OTEL plumbing — isolated tracer/exporter per test (matches
# tests/telemetry/test_confrontation_intent_spans.py convention)
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


def _make_dispatch(
    *,
    subsystem: str,
    params: dict[str, Any],
    idempotency_key: str = "k1",
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem=subsystem,
        params=params,
        idempotency_key=idempotency_key,
        confidence=1.0,
        visibility=_open_viz(),
    )


def _package_with(
    *dispatches: SubsystemDispatch,
    turn_id: str = "turn-1",
) -> DispatchPackage:
    """One-player package carrying the supplied dispatches."""
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="(synthetic for watcher test)",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _empty_package(turn_id: str = "turn-1") -> DispatchPackage:
    """Package with no dispatches — quiet turn baseline."""
    return DispatchPackage(turn_id=turn_id, confidence_global=1.0)


def _make_encounter(encounter_type: str) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type=encounter_type,
        player_metric=EncounterMetric(name="player", threshold=10),
        opponent_metric=EncounterMetric(name="opponent", threshold=10),
    )


def _make_magic_state(actors_with_working: list[str] | None = None) -> MagicState:
    """Construct a minimal MagicState. If ``actors_with_working`` is provided,
    append one WorkingRecord per actor to the working_log to simulate the
    engine having fired this turn."""
    config = WorldMagicConfig(
        world_slug="test_world",
        genre_slug="test_genre",
        allowed_sources=["innate"],
        active_plugins=["innate_v1"],
        intensity=0.25,
        world_knowledge=WorldKnowledge(primary="classified", local_register="folkloric"),
        visibility={"primary": "feared"},
        hard_limits=[HardLimit(id="hl", description="test")],
        cost_types=["sanity"],
        ledger_bars=[
            LedgerBarSpec(
                id="sanity",
                scope="character",
                direction="down",
                range=(0.0, 1.0),
                threshold_low=0.40,
                consequence_on_low_cross="x",
                starts_at_chargen=1.0,
            ),
        ],
        narrator_register="x",
    )
    state = MagicState.from_config(config)
    for actor in actors_with_working or []:
        state.working_log.append(
            WorkingRecord(
                plugin="innate_v1",
                mechanism="test",
                actor=actor,
                costs={"sanity": 0.1},
                domain="test",
                narrator_basis="test",
            )
        )
    return state


def _snapshot(
    *,
    encounter: StructuredEncounter | None = None,
    magic_state: MagicState | None = None,
    scenario_state: ScenarioState | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        encounter=encounter,
        magic_state=magic_state,
        scenario_state=scenario_state,
    )


# ---------------------------------------------------------------------------
# AC1 — confrontation dispatched but no encounter on snapshot → mismatch span
# ---------------------------------------------------------------------------


def test_confrontation_dispatched_with_no_encounter_emits_mismatch_span() -> None:
    """AC1: Router dispatched ``confrontation:negotiation`` + snapshot has no
    encounter → mismatch span fires.

    The watcher observes that the router said "engage confrontation" but the
    engine left snapshot.encounter == None. This is the convincing-prose-
    without-mechanical-backing failure mode the watcher exists to surface.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(subsystem="confrontation", params={"type": "negotiation"})
    )
    snap = _snapshot(encounter=None)

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1, (
        f"expected exactly 1 mismatch span, got {len(spans)}: {[s.name for s in spans]}"
    )
    assert spans[0].name == "dispatch_engagement.confrontation.mismatch"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("subsystem") == "confrontation"


def test_confrontation_dispatched_with_wrong_encounter_kind_emits_mismatch_span() -> None:
    """AC1 edge: encounter exists but kind disagrees with dispatched type →
    still a mismatch (kind divergence is the same lie).

    Dispatched ``confrontation:negotiation`` but snapshot has a ``duel``
    encounter — the engine fired SOMETHING but not the right thing. Watcher
    must surface this; otherwise a router that always dispatches negotiation
    but lets the engine roll combat would silently slip past.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(subsystem="confrontation", params={"type": "negotiation"})
    )
    snap = _snapshot(encounter=_make_encounter("duel"))

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.confrontation.mismatch"


# ---------------------------------------------------------------------------
# AC2 — confrontation dispatched + matching encounter → NO span
# ---------------------------------------------------------------------------


def test_confrontation_dispatched_with_matching_encounter_emits_no_span() -> None:
    """AC2: Router dispatched ``confrontation:negotiation`` + matching encounter
    → no mismatch span (no false positive on legitimate engagement)."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(subsystem="confrontation", params={"type": "negotiation"})
    )
    snap = _snapshot(encounter=_make_encounter("negotiation"))

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert spans == [], f"expected zero mismatch spans, got: {[s.name for s in spans]}"


# ---------------------------------------------------------------------------
# AC3 — quiet turn (no dispatch + no engagement) → NO span
# ---------------------------------------------------------------------------


def test_quiet_turn_no_dispatch_no_engagement_emits_no_span() -> None:
    """AC3: Router dispatched nothing + snapshot has no engagement → no span.

    This is the SOUL "Cost Scales with Drama" / "quiet walk through town"
    case. A noisy lie-detector that fires on every uneventful turn would
    drown out the real mismatches — the watcher MUST stay silent here."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    snap = _snapshot()  # no encounter, no magic_state, no scenario_state

    run_dispatch_engagement_watcher(package=_empty_package(), snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert spans == []


def test_quiet_turn_with_existing_engagement_emits_no_span() -> None:
    """Edge case (not stated as a numbered AC but implied by the watcher
    contract): an encounter exists but nothing was dispatched. The watcher
    must NOT fire — the encounter could have been carried over from a
    prior turn. Dispatching "nothing" is not a lie if nothing changed."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    snap = _snapshot(encounter=_make_encounter("negotiation"))

    run_dispatch_engagement_watcher(package=_empty_package(), snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert spans == []


def test_watcher_no_op_when_package_is_none() -> None:
    """Pre-59-4 reality (story context Assumption): on the live SDK path
    ``turn_context.dispatch_package`` is None until 59-4 wires the router.
    The watcher must be safe to call with ``package=None`` — no-op, no
    span, no crash. Without this guard the post-turn hook would explode
    on every turn between 59-3 ship and 59-4 ship."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    snap = _snapshot()

    run_dispatch_engagement_watcher(package=None, snapshot=snap, tracer=tracer)

    assert len(exporter.get_finished_spans()) == 0


# ---------------------------------------------------------------------------
# AC4 — magic_working + scenario_clue dispatch vocabulary coverage
# ---------------------------------------------------------------------------


def test_magic_working_dispatched_with_no_engine_record_emits_mismatch_span() -> None:
    """AC4: router dispatched ``magic_working`` for actor Hilda but
    ``snapshot.magic_state.working_log`` has no record for Hilda → mismatch.

    Engagement witness: a WorkingRecord with ``actor == params["actor"]``
    must exist in working_log. Empty log = engine did not fire = lie."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_make_dispatch(subsystem="magic_working", params={"actor": "Hilda"}))
    snap = _snapshot(magic_state=_make_magic_state())  # empty working_log

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.magic_working.mismatch"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("subsystem") == "magic_working"


def test_magic_working_dispatched_with_no_magic_state_emits_mismatch_span() -> None:
    """AC4 edge: ``snapshot.magic_state is None`` (world has no magic config)
    yet the router dispatched magic_working. Engine COULDN'T have fired —
    still a lie, still a mismatch."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_make_dispatch(subsystem="magic_working", params={"actor": "Hilda"}))
    snap = _snapshot(magic_state=None)

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.magic_working.mismatch"


def test_magic_working_dispatched_with_matching_record_emits_no_span() -> None:
    """AC4 happy path: actor Hilda's WorkingRecord landed → engine engaged →
    no span."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_make_dispatch(subsystem="magic_working", params={"actor": "Hilda"}))
    snap = _snapshot(magic_state=_make_magic_state(actors_with_working=["Hilda"]))

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert spans == []


def test_magic_working_dispatched_with_wrong_actor_record_emits_mismatch_span() -> None:
    """AC4 edge: a WorkingRecord exists but for the WRONG actor. Dispatched
    Hilda; engine fired for Bjorn. Still a mismatch — the engine engaged
    for the wrong subject."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_make_dispatch(subsystem="magic_working", params={"actor": "Hilda"}))
    snap = _snapshot(magic_state=_make_magic_state(actors_with_working=["Bjorn"]))

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.magic_working.mismatch"


def test_scenario_clue_dispatched_with_no_discovery_emits_mismatch_span() -> None:
    """AC4: router dispatched ``scenario_clue`` for fact_id ``evidence-X`` but
    ``snapshot.scenario_state.discovered_clues`` lacks the id → mismatch.

    Engagement witness: ``params["fact_id"] in discovered_clues``."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(subsystem="scenario_clue", params={"fact_id": "evidence-X"})
    )
    snap = _snapshot(scenario_state=ScenarioState())  # default empty discovered_clues

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.scenario_clue.mismatch"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("subsystem") == "scenario_clue"


def test_scenario_clue_dispatched_with_no_scenario_state_emits_mismatch_span() -> None:
    """AC4 edge: ``snapshot.scenario_state is None`` but scenario_clue was
    dispatched. Engine couldn't have advanced anything — still a lie."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(subsystem="scenario_clue", params={"fact_id": "evidence-X"})
    )
    snap = _snapshot(scenario_state=None)

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.scenario_clue.mismatch"


def test_scenario_clue_dispatched_with_discovery_emits_no_span() -> None:
    """AC4 happy path: clue was discovered → engine engaged → no span."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(subsystem="scenario_clue", params={"fact_id": "evidence-X"})
    )
    scenario = ScenarioState()
    scenario.discovered_clues.add("evidence-X")
    snap = _snapshot(scenario_state=scenario)

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert spans == []


def test_multiple_mismatches_emit_one_span_per_mismatch() -> None:
    """A single turn with three dispatches that all fail to engage emits three
    distinct spans — not one aggregated "turn had problems" span, and not
    just the first one. The GM panel needs per-subsystem accountability."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(
            subsystem="confrontation",
            params={"type": "negotiation"},
            idempotency_key="k1",
        ),
        _make_dispatch(
            subsystem="magic_working",
            params={"actor": "Hilda"},
            idempotency_key="k2",
        ),
        _make_dispatch(
            subsystem="scenario_clue",
            params={"fact_id": "evidence-X"},
            idempotency_key="k3",
        ),
    )
    snap = _snapshot(magic_state=_make_magic_state(), scenario_state=ScenarioState())

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    span_names = sorted(
        s.name for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name
    )
    assert span_names == [
        "dispatch_engagement.confrontation.mismatch",
        "dispatch_engagement.magic_working.mismatch",
        "dispatch_engagement.scenario_clue.mismatch",
    ]


def test_partial_engagement_emits_spans_only_for_unengaged_dispatches() -> None:
    """Mixed turn: confrontation engaged correctly, magic_working did not.
    Watcher emits exactly ONE span — for the magic mismatch — and stays
    silent on the confrontation that did its job."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(
            subsystem="confrontation",
            params={"type": "negotiation"},
            idempotency_key="k1",
        ),
        _make_dispatch(
            subsystem="magic_working",
            params={"actor": "Hilda"},
            idempotency_key="k2",
        ),
    )
    snap = _snapshot(
        encounter=_make_encounter("negotiation"),
        magic_state=_make_magic_state(),  # empty — no Hilda working
    )

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.magic_working.mismatch"


def test_cross_player_dispatches_also_watched() -> None:
    """``DispatchPackage.cross_player`` is the other dispatch site (alongside
    ``per_player``). The watcher must cover both — a router that mismatches
    on a cross-player confrontation (e.g. PvP scandal) would otherwise slip
    past the watcher silently."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )
    from sidequest.protocol.dispatch import CrossAction

    tracer, exporter = _fresh_tracer_and_exporter()
    package = DispatchPackage(
        turn_id="turn-1",
        cross_player=[
            CrossAction(
                participants=["player:Alice", "player:Bob"],
                witnesses=["player:Alice", "player:Bob"],
                dispatch=[
                    _make_dispatch(
                        subsystem="confrontation",
                        params={"type": "social_duel"},
                        idempotency_key="cx1",
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )
    snap = _snapshot(encounter=None)

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.confrontation.mismatch"


# ---------------------------------------------------------------------------
# Fail-loud discipline (SOUL "No Silent Fallbacks", project memory
# feedback_no_fallbacks_hard)
# ---------------------------------------------------------------------------


def test_watcher_surfaces_malformed_confrontation_dispatch_as_mismatch_not_crash() -> None:
    """A confrontation dispatch with no ``params["type"]`` is a producer bug.

    Corrected contract (playtest 2026-05-25): the watcher runs POST-narration
    in the WS turn pipeline, so an uncaught ``KeyError`` here crashes turn
    *delivery* and hangs the MP table (the narration had already succeeded).
    Fail-loud done right surfaces the defect as a ``dispatch_engagement``
    **mismatch span** — the loudest channel that actually reaches the GM
    panel — WITHOUT raising. A crash that prevents the span from exporting is
    the silent-worst mode, not a loud one (memory feedback_no_fallbacks_hard).
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(subsystem="confrontation", params={})  # missing "type"
    )
    snap = _snapshot()

    # MUST NOT raise (crash would close the WS and hang the turn).
    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1, f"expected 1 malformed-dispatch mismatch span, got {len(spans)}"
    assert spans[0].name == "dispatch_engagement.confrontation.mismatch"
    assert "params['type']" in str(dict(spans[0].attributes or {}).get("evidence", "")), (
        "mismatch evidence must name the missing required param key so the GM "
        "panel shows exactly what the router omitted"
    )


def test_watcher_surfaces_malformed_magic_working_dispatch_as_mismatch_not_crash() -> None:
    """Same corrected contract for magic_working (missing ``actor``)."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_make_dispatch(subsystem="magic_working", params={}))
    snap = _snapshot()

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.magic_working.mismatch"
    assert "params['actor']" in str(dict(spans[0].attributes or {}).get("evidence", ""))


def test_watcher_surfaces_malformed_scenario_clue_dispatch_as_mismatch_not_crash() -> None:
    """Same corrected contract for scenario_clue (missing ``fact_id``)."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(_make_dispatch(subsystem="scenario_clue", params={}))
    snap = _snapshot()

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.scenario_clue.mismatch"
    assert "params['fact_id']" in str(dict(spans[0].attributes or {}).get("evidence", ""))


# ---------------------------------------------------------------------------
# Pure-function discipline — detect function returns mismatches without
# emitting OTEL, so callers can compose / test in isolation. Mirrors the
# pure ``validate()`` / OTEL-emitting wrapper split in
# confrontation_intent_validator.py (the file 59-3 repurposes).
# ---------------------------------------------------------------------------


def test_detect_function_exists_and_is_pure() -> None:
    """The pure ``detect_dispatch_engagement_mismatch(package, snapshot)``
    function returns a list of mismatch records WITHOUT emitting OTEL spans.

    This separation is load-bearing for testability (callers can introspect
    decisions without an OTEL exporter) AND for the wiring split — the
    OTEL-emitting wrapper is a thin shell over the pure decision."""
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    _, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(subsystem="confrontation", params={"type": "negotiation"})
    )
    snap = _snapshot()

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    # Returns at least one mismatch (snapshot has no encounter).
    assert len(mismatches) >= 1
    # No OTEL spans emitted — the pure function does not touch the tracer.
    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert spans == []


def test_detect_function_returns_subsystem_on_mismatch_record() -> None:
    """Each mismatch record carries enough information for the wrapper to
    emit a useful span. Minimum contract: subsystem is identifiable."""
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    package = _package_with(
        _make_dispatch(
            subsystem="magic_working",
            params={"actor": "Hilda"},
            idempotency_key="kkk",
        )
    )
    snap = _snapshot(magic_state=_make_magic_state())

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)
    assert len(mismatches) == 1
    # Either an attribute or dict access — the watcher's record type is
    # Dev's call. We probe both common shapes.
    m = mismatches[0]
    subsystem = getattr(m, "subsystem", None) or (
        m.get("subsystem") if isinstance(m, dict) else None
    )
    assert subsystem == "magic_working"


# ---------------------------------------------------------------------------
# AC5 — Wiring (per CLAUDE.md "Every Test Suite Needs a Wiring Test",
# "No Source-Text Wiring Tests" → reflection + behavior)
# ---------------------------------------------------------------------------


def test_watcher_module_exports_public_api() -> None:
    """Wiring tripwire #1: the production module exports both the pure
    decision function and the OTEL-emitting wrapper at the names the
    handler will import. Catches a broken __all__ or a module rename
    before any handler-side test runs."""
    from sidequest.agents import dispatch_engagement_watcher as mod

    assert hasattr(mod, "detect_dispatch_engagement_mismatch"), (
        "module must export detect_dispatch_engagement_mismatch"
    )
    assert hasattr(mod, "run_dispatch_engagement_watcher"), (
        "module must export run_dispatch_engagement_watcher"
    )
    assert callable(mod.detect_dispatch_engagement_mismatch)
    assert callable(mod.run_dispatch_engagement_watcher)


def test_watcher_wired_into_session_handler() -> None:
    """Wiring tripwire #2 (per CLAUDE.md sanctioned alternatives to source-
    grep): the handler module's runtime namespace must contain a reference
    to ``run_dispatch_engagement_watcher``. Reflection on ``module.__dict__``
    catches "Dev forgot to import the watcher in the handler" — the exact
    silent-failure mode that 59-3 exists to prevent.

    Imports via ``sidequest.server.session_handler`` (the public surface
    that re-exports ``WebSocketSessionHandler``) to avoid tripping the
    pre-existing circular import between ``session_handler.py`` and
    ``websocket_session_handler.py``. Once ``session_handler`` is loaded,
    ``websocket_session_handler`` is also in ``sys.modules`` and its
    runtime namespace is inspectable."""
    import sys

    import sidequest.server.session_handler  # noqa: F401 — load order fix

    handler_mod = sys.modules["sidequest.server.websocket_session_handler"]

    # Either a top-level import (``run_dispatch_engagement_watcher`` in the
    # module namespace) or via a re-export of the module itself
    # (``dispatch_engagement_watcher.run_dispatch_engagement_watcher``).
    # Both shapes prove the handler can call it; neither requires source-
    # text inspection.
    has_function = "run_dispatch_engagement_watcher" in handler_mod.__dict__
    has_module = "dispatch_engagement_watcher" in handler_mod.__dict__
    assert has_function or has_module, (
        "websocket_session_handler must import the dispatch engagement "
        "watcher (either the function directly or the module). Found "
        "neither. The post-turn watcher call site is missing — without "
        "this import the watcher cannot fire in production."
    )


def test_watcher_fixture_round_trip_through_real_pipeline() -> None:
    """AC5 (the literal AC text): drive a synthetic router-dispatched-not-
    engaged turn through the real watcher hook and assert span emission.

    "Real" here = the actual public function from production module, no
    test doubles for the watcher itself. Constructs a real DispatchPackage,
    a real GameSnapshot, and invokes the real wrapper. If the function or
    its OTEL emission path is broken at any point in the chain, this test
    catches it without needing the entire websocket session machinery.

    Mirrors the canonical fixture-driven wiring pattern at
    tests/server/test_location_description_emit.py::test_emit_sends_message_when_room_has_manifest."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _package_with(
        _make_dispatch(
            subsystem="confrontation",
            params={"type": "negotiation"},
            idempotency_key="round-trip-1",
        )
    )
    snap = _snapshot(encounter=None)

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = exporter.get_finished_spans()
    confrontation_spans = [
        s for s in spans if s.name == "dispatch_engagement.confrontation.mismatch"
    ]
    assert len(confrontation_spans) == 1, (
        f"real-pipeline fixture should emit exactly one confrontation mismatch "
        f"span; got spans: {[s.name for s in spans]}"
    )
