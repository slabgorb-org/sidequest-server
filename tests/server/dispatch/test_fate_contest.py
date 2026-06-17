"""Behavior tests for the Fate Contest engine (spec 2026-06-17 §2/§5).

A Contest resolves a *goal*, not harm: opposed 4dF, best total per side scores a
victory (2 on a >=3 margin), a tie grants each top side a boost; first side to
``contest.target`` victories wins. These four tests are the core spec (§5)
verification — each asserts real victory/margin/first-to-N/tie->boost math.

Fixture note (reconciled against real code): the engine seats the opponent via
``_seat_opponent_commits``, which makes the opponent ATTACK with its mental-track
skill (``Provoke``). The Vicar has no Provoke (rating 0), so its seated roll is a
pure 4dF. ``random.Random(0)`` rolls the opponent's first (and only) 4dF to a sum
of -1 (faces ``[0, 0, -1, 0]``); the player's total is the value the test seals.
Each test sets the player's sealed ``ladder_total`` so the intended branch fires
by real math — the brief's "all -1 -> -4" comments were wrong about Random(0), so
the sealed totals here are chosen against the actual rolled opponent total of -1.
"""

from __future__ import annotations

import random

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import (
    ContestState,
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot, Npc
from sidequest.server.dispatch.fate_conflict import seal_fate_commit
from sidequest.server.dispatch.fate_contest import (
    FateContestError,
    run_fate_contest_exchange,
)


class _ZeroDice(random.Random):
    """4dF that always rolls 0 on every face -> ladder_total == skill_rating."""

    def choice(self, seq):  # type: ignore[override]
        return 0


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _contest_encounter() -> StructuredEncounter:
    enc = StructuredEncounter(
        encounter_type="negotiation",
        category="social",
        player_metric=EncounterMetric(name="leverage", threshold=3),
        opponent_metric=EncounterMetric(name="leverage", threshold=3),
        actors=[
            EncounterActor(name="Lady Ash", role="lead", side="player"),
            EncounterActor(name="The Vicar", role="rival", side="opponent"),
        ],
    )
    enc.contest = ContestState(target=3)
    return enc


def _snapshot(enc: StructuredEncounter) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="tea_test",
        characters=[_pc("Lady Ash", {"Rapport": 4})],
        npcs=[
            Npc(
                core=CreatureCore(
                    name="The Vicar",
                    description="d",
                    personality="p",
                    # No Provoke rating: the opponent's seated mental attack rolls a
                    # pure 4dF (rating 0), so the opponent total is fully controlled
                    # by the rng and the tests can set the margin via the player's
                    # sealed total alone.
                    fate_sheet=FateSheet(skills={"Rapport": 1, "Empathy": 1}),
                )
            )
        ],
        encounter=enc,
    )
    return snap


def test_higher_total_scores_one_victory():
    # Opponent rolls Random(0) -> 4dF sum -1, Provoke 0 -> total -1. Player seals 1
    # -> margin 1 - (-1) = 2 (< 3) -> exactly +1 victory.
    enc = _contest_encounter()
    snap = _snapshot(enc)
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Lady Ash"),
        action="overcome",
        skill="Rapport",
        difficulty=0,
        ladder_total=1,
    )
    result = run_fate_contest_exchange(
        encounter=enc,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        rng=random.Random(0),
        round_number=1,
    )
    assert enc.contest.player_victories == 1
    assert enc.contest.opponent_victories == 0
    assert result.resolved is False
    assert enc.fate_commits == []  # ledger cleared


def test_margin_of_three_scores_two_victories():
    # Opponent -1; player seals 8 -> margin 9 >= 3 -> +2 (succeed with style).
    enc = _contest_encounter()
    snap = _snapshot(enc)
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Lady Ash"),
        action="overcome",
        skill="Rapport",
        difficulty=0,
        ladder_total=8,
    )
    run_fate_contest_exchange(
        encounter=enc,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        rng=random.Random(0),
        round_number=1,
    )
    assert enc.contest.player_victories == 2
    assert enc.contest.opponent_victories == 0


def test_first_to_three_resolves_with_winner_outcome():
    # Start at 2 victories; opponent -1; player seals 9 -> +2 -> 4 >= target 3.
    enc = _contest_encounter()
    enc.contest = ContestState(target=3, player_victories=2)
    snap = _snapshot(enc)
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Lady Ash"),
        action="overcome",
        skill="Rapport",
        difficulty=0,
        ladder_total=9,
    )
    result = run_fate_contest_exchange(
        encounter=enc,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        rng=random.Random(0),
    )
    assert result.resolved is True
    assert enc.resolved is True
    assert enc.outcome == "player_victory"


def test_opponent_reaching_target_resolves_with_opponent_victory():
    """Minor (Westley): the opponent-win path. The opponent crosses the target and
    the contest resolves as ``opponent_victory`` — symmetric to the player path."""
    enc = _contest_encounter()
    enc.contest = ContestState(target=3, opponent_victories=2)  # one opp win ends it
    snap = _snapshot(enc)
    # Player seals a LOW total so the opponent (Random(0) -> 4dF sum -1, +0 Provoke
    # = -1) wins the exchange. Player seals -3 -> margin (-1) - (-3) = 2 -> +1 opp
    # victory -> tally 3 >= target 3 -> opponent_victory.
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Lady Ash"),
        action="overcome",
        skill="Rapport",
        difficulty=0,
        ladder_total=-3,
    )

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("oppwin")

    result = run_fate_contest_exchange(
        encounter=enc,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        rng=random.Random(0),
        _tracer=tracer,
    )
    assert enc.contest.opponent_victories >= enc.contest.target
    assert result.resolved is True
    assert enc.outcome == "opponent_victory"
    span_names = {s.name for s in exporter.get_finished_spans()}
    # Both resolution spans fire on the opponent-win path too.
    assert "fate.contest.exchange" in span_names
    assert "fate.contest.resolved" in span_names


def test_tie_grants_each_side_a_boost_and_no_victory():
    # _ZeroDice -> opponent 4dF sum 0, Provoke 0 -> opponent total 0. Player seals 0
    # -> 0 == 0 tie: no victory, each top side gains a boost.
    enc = _contest_encounter()
    snap = _snapshot(enc)
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Lady Ash"),
        action="overcome",
        skill="Rapport",
        difficulty=0,
        ladder_total=0,
    )
    run_fate_contest_exchange(
        encounter=enc,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        rng=_ZeroDice(),
    )
    assert enc.contest.player_victories == 0
    assert enc.contest.opponent_victories == 0
    assert any(a.kind == "boost" for a in enc.situation_aspects)
    assert len([a for a in enc.situation_aspects if a.kind == "boost"]) == 2


def test_non_contest_encounter_fails_loud():
    """run_fate_contest_exchange on a Conflict (contest is None) raises — this is
    a Conflict, route it to run_fate_exchange (No Silent Fallbacks)."""
    enc = StructuredEncounter(
        encounter_type="duel",
        category="social",
        player_metric=EncounterMetric(name="p", threshold=3),
        opponent_metric=EncounterMetric(name="o", threshold=3),
        actors=[EncounterActor(name="Lady Ash", role="lead", side="player")],
    )
    snap = _snapshot(enc)

    with pytest.raises(FateContestError):
        run_fate_contest_exchange(
            encounter=enc,
            snapshot=snap,
            ruleset=get_ruleset_module("fate"),
            rng=random.Random(0),
        )


def test_seating_stamps_contest_state_and_emits_span():
    """instantiate_encounter_from_trigger stamps encounter.contest for a
    contest-mode cdef and fires fate.contest.seeded (the GM-panel wiring proof).

    Fixture shape: a lightweight pack stand-in (SimpleNamespace with a RulesConfig
    carrying a single contest-mode ConfrontationDef) — same pattern used by
    tests/server/dispatch/test_table_instantiation.py. The span capture installs an
    InMemorySpanExporter on the global tracer provider (same pattern as
    test_resolution_turn_same_type_suppresses_initiated_span in
    tests/server/test_encounter_lifecycle.py).
    """
    from types import SimpleNamespace

    import opentelemetry.trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.genre.models.rules import (
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        WinCondition,
    )
    from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger
    from sidequest.telemetry.setup import init_tracer

    # Install in-memory span exporter onto the global provider (idempotent init_tracer).
    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Build a contest-mode ConfrontationDef with player_metric.threshold=3.
    cdef = ConfrontationDef(
        type="negotiation",
        label="Polite Negotiation",
        category="social",
        resolution_mode=ResolutionMode.contest,
        win_condition=WinCondition.dial_threshold,
        player_metric=MetricDef(name="leverage", threshold=3),
        opponent_metric=MetricDef(name="leverage", threshold=3),
    )
    # Lightweight pack stand-in: encounter_lifecycle only reads pack.rules
    # (find_confrontation_def) on the standard social/dial_threshold path.
    # Use ruleset="dial" — RulesConfig(ruleset="fate") requires a FateConfig block,
    # and pack.rules.ruleset is never accessed for a social contest seat anyway
    # (no initiative roll, no edge publish on the non-combat, non-hp_depletion path).
    pack = SimpleNamespace(rules=RulesConfig(ruleset="dial", confrontations=[cdef]))

    pc = Character(
        core=CreatureCore(name="Lady Ash", description="d", personality="p"),
        char_class="Agent",
        race="Human",
        backstory="b",
    )
    vicar = Npc(core=CreatureCore(name="The Vicar", description="d", personality="p"))
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[pc],
        npcs=[vicar],
    )

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,  # type: ignore[arg-type]
        encounter_type="negotiation",
        player_name="Lady Ash",
        npcs_present=[NpcMention(name="The Vicar", side="opponent", role="rival")],
        genre_slug="fate_test",
    )

    assert enc is not None, "instantiate_encounter_from_trigger returned None"
    # AC-1: contest state is stamped.
    assert enc.contest is not None, "encounter.contest must not be None for contest-mode cdef"
    # AC-2: target comes from cdef.player_metric.threshold.
    assert enc.contest.target == 3, (
        f"expected target=3 from cdef.player_metric.threshold; got {enc.contest.target}"
    )
    # AC-3: fate.contest.seeded span fired.
    span_names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.contest.seeded" in span_names, (
        f"expected fate.contest.seeded span; got spans: {span_names}"
    )


def test_seating_honors_authored_victory_head_start():
    """Westley major M2: a content author can give a side a victory head-start via
    ``metric.starting`` (tea_and_murder negotiation opponent starts at 1; scandal at
    2). Seating MUST seed ContestState.{player,opponent}_victories from it — otherwise
    the authored asymmetry vanishes silently (No Silent Fallbacks). The existing
    seating test uses the default starting=0; this covers the non-zero case."""
    from types import SimpleNamespace

    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.genre.models.rules import (
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        WinCondition,
    )
    from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger

    # opponent gets a 1-victory head-start (the negotiation shape); player gets 0.
    cdef = ConfrontationDef(
        type="negotiation",
        label="Polite Negotiation",
        category="social",
        resolution_mode=ResolutionMode.contest,
        win_condition=WinCondition.dial_threshold,
        player_metric=MetricDef(name="leverage", starting=0, threshold=3),
        opponent_metric=MetricDef(name="leverage", starting=1, threshold=3),
    )
    pack = SimpleNamespace(rules=RulesConfig(ruleset="dial", confrontations=[cdef]))

    pc = Character(
        core=CreatureCore(name="Lady Ash", description="d", personality="p"),
        char_class="Agent",
        race="Human",
        backstory="b",
    )
    vicar = Npc(core=CreatureCore(name="The Vicar", description="d", personality="p"))
    snap = GameSnapshot(genre_slug="fate_test", characters=[pc], npcs=[vicar])

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,  # type: ignore[arg-type]
        encounter_type="negotiation",
        player_name="Lady Ash",
        npcs_present=[NpcMention(name="The Vicar", side="opponent", role="rival")],
        genre_slug="fate_test",
    )

    assert enc is not None and enc.contest is not None
    assert enc.contest.target == 3
    # The authored head-start is seeded into the tally — NOT silently dropped.
    assert enc.contest.opponent_victories == cdef.opponent_metric.starting == 1, (
        f"opponent head-start (starting={cdef.opponent_metric.starting}) must seed "
        f"ContestState.opponent_victories; got {enc.contest.opponent_victories}"
    )
    assert enc.contest.player_victories == cdef.player_metric.starting == 0


# ---------------------------------------------------------------------------
# Task 7: dispatch_fate_action routes to the Contest engine (spec 2026-06-17 §2)
# ---------------------------------------------------------------------------

from sidequest.protocol.fate import FateActionPayload  # noqa: E402
from sidequest.server.dispatch.fate_conflict import (  # noqa: E402
    FateConflictError,
    dispatch_fate_action,
)
from sidequest.server.dispatch.fate_contest import FateContestResult  # noqa: E402


def _payload(action: str, skill: str = "Rapport") -> FateActionPayload:
    return FateActionPayload(request_id="r1", action=action, skill=skill, difficulty=0)


def test_dispatch_rejects_attack_in_a_contest():
    enc = _contest_encounter()
    snap = _snapshot(enc)
    with pytest.raises(FateConflictError, match="Contest"):
        dispatch_fate_action(
            payload=_payload("attack"),
            actor_name="Lady Ash",
            encounter=enc,
            ruleset=get_ruleset_module("fate"),
            snapshot=snap,
            rng=_ZeroDice(),
        )


def test_dispatch_runs_contest_engine_when_barrier_closes():
    enc = _contest_encounter()
    snap = _snapshot(enc)  # 1 PC seated -> a single overcome closes the barrier
    result = dispatch_fate_action(
        payload=_payload("overcome"),
        actor_name="Lady Ash",
        encounter=enc,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap,
        rng=_ZeroDice(),
    )
    assert result.commitment_pending is False
    assert isinstance(result.exchange, FateContestResult)


# ---------------------------------------------------------------------------
# Task 12: §0 no-bleed proof — contest turn fires fate.contest.* and never
# touches the dial engine (spec 2026-06-17 §0/§5)
# ---------------------------------------------------------------------------


def test_contest_turn_fires_contest_spans_and_no_dial(monkeypatch):
    """spec §0/§5 no-bleed proof: a Fate contest exchange resolves through the
    Contest engine — fate.contest.* spans fire and the dial engine is never
    reached (no get_ruleset_module('dial'/'native') call during the turn)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import sidequest.game.ruleset as ruleset_pkg
    import sidequest.server.dispatch.fate_conflict as fate_conflict_pkg

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("nobleed")

    # Obtain the Fate ruleset BEFORE installing the guard so the legitimate
    # "fate" lookup does not trip the tripwire.
    fate_ruleset = get_ruleset_module("fate")

    # Tripwire: fail if anything resolves the dial engine during the turn.
    real_get = ruleset_pkg.get_ruleset_module

    def _guarded_get(slug):
        assert slug not in ("dial", "native"), (
            f"dial engine reached on a Fate contest path (slug={slug!r}) — the bleed "
            "the spec closes (spec 2026-06-17 §0)"
        )
        return real_get(slug)

    monkeypatch.setattr(ruleset_pkg, "get_ruleset_module", _guarded_get)

    # Hardened tripwire (Westley minor): a contest exchange must resolve through the
    # Contest engine ONLY — it must NEVER fall through to the Conflict engine
    # (run_fate_exchange). Make the Conflict entrypoint blow up so a regression that
    # routes a contest turn into the Conflict path trips here, not silently in prod.
    def _conflict_must_not_fire(*_a, **_kw):
        raise AssertionError(
            "run_fate_exchange (the Conflict engine) was called on a Contest turn — "
            "the contest must resolve via run_fate_contest_exchange only (spec §0)"
        )

    monkeypatch.setattr(fate_conflict_pkg, "run_fate_exchange", _conflict_must_not_fire)

    enc = _contest_encounter()
    enc.contest = ContestState(target=3, player_victories=2)  # one win ends it
    snap = _snapshot(enc)
    result = dispatch_fate_action(
        payload=_payload("overcome"),
        actor_name="Lady Ash",
        encounter=enc,
        ruleset=fate_ruleset,
        snapshot=snap,
        rng=_ZeroDice(),
        _tracer=tracer,
    )
    names = {s.name for s in exporter.get_finished_spans()}
    assert "fate.contest.exchange" in names
    assert result.exchange is not None
    assert result.exchange.resolved is True and "fate.contest.resolved" in names
    assert not any(".compute_dc" in n or n.endswith(".dial") for n in names)


def test_contest_resolution_fires_universal_encounter_resolved_span():
    """3b wiring test: a Fate Contest points-win fires BOTH fate.contest.resolved
    (genre layer) AND encounter.resolved (platform substrate — render trigger,
    forensic-timeline, input-unlock). The contest path must not drop the universal
    teardown signal that every other resolution engine emits."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from sidequest.telemetry.spans.encounter import SPAN_ENCOUNTER_RESOLVED

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("wiring")

    enc = _contest_encounter()
    enc.contest = ContestState(target=3, player_victories=2)  # one win ends it
    snap = _snapshot(enc)
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Lady Ash"),
        action="overcome",
        skill="Rapport",
        difficulty=0,
        ladder_total=9,
    )
    run_fate_contest_exchange(
        encounter=enc,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        rng=random.Random(0),
        _tracer=tracer,
    )

    span_names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.contest.resolved" in span_names, (
        f"fate.contest.resolved span must fire on contest resolution; got {span_names}"
    )
    assert SPAN_ENCOUNTER_RESOLVED in span_names, (
        "encounter.resolved (universal substrate signal) must ALSO fire on contest "
        f"resolution — render trigger and forensic-timeline key on it; got {span_names}"
    )
    assert enc.resolved is True


def test_resolution_signal_reports_contest_tally_not_frozen_metric():
    """Westley major M3: on the contest path the dial metrics are seeded then NEVER
    advanced — the engine only touches contest.player_victories/opponent_victories.
    The resolution signal (and its OTEL span — the GM-panel lie detector) MUST report
    the live victory tally, not the frozen start value of the EncounterMetric."""
    import opentelemetry.trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from sidequest.telemetry.setup import init_tracer
    from sidequest.telemetry.spans.encounter import (
        SPAN_ENCOUNTER_RESOLUTION_SIGNAL_EMITTED,
    )

    # _build_resolution_signal emits encounter.resolution_signal_emitted on the GLOBAL
    # tracer (Span.open), so capture on the global provider — same pattern as the
    # seating test (test_seating_stamps_contest_state_and_emits_span).
    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    enc = _contest_encounter()
    # Frozen start values on the dial metrics — these MUST NOT be what gets reported.
    assert enc.player_metric.current == 0
    assert enc.opponent_metric.current == 0
    enc.contest = ContestState(target=3, player_victories=2)  # one win ends it -> 3
    snap = _snapshot(enc)
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Lady Ash"),
        action="overcome",
        skill="Rapport",
        difficulty=0,
        ladder_total=9,
    )
    run_fate_contest_exchange(
        encounter=enc,
        snapshot=snap,
        ruleset=get_ruleset_module("fate"),
        rng=random.Random(0),
    )

    assert enc.resolved is True
    # The built signal is stashed on the snapshot — it must carry the victory tally.
    signal = snap.pending_resolution_signal
    assert signal is not None, "contest resolution must build a ResolutionSignal"
    assert signal.final_player_metric == enc.contest.player_victories, (
        f"final_player_metric must be the contest tally "
        f"({enc.contest.player_victories}), not the frozen metric "
        f"({enc.player_metric.current}); got {signal.final_player_metric}"
    )
    assert signal.final_opponent_metric == enc.contest.opponent_victories
    # The frozen metric is NOT what was reported (the bug M3 fixes).
    assert signal.final_player_metric != enc.player_metric.current

    # And the GM-panel span carries the same tally (the lie detector).
    sig_spans = [
        s
        for s in exporter.get_finished_spans()
        if s.name == SPAN_ENCOUNTER_RESOLUTION_SIGNAL_EMITTED
    ]
    assert sig_spans, "encounter.resolution_signal_emitted span must fire on contest path"
    span = sig_spans[-1]
    assert span.attributes["final_player_metric"] == enc.contest.player_victories
    assert span.attributes["final_opponent_metric"] == enc.contest.opponent_victories
    assert enc.outcome == "player_victory"
