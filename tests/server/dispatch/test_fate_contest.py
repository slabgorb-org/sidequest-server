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
    import pytest

    with pytest.raises(FateContestError):
        run_fate_contest_exchange(
            encounter=enc,
            snapshot=snap,
            ruleset=get_ruleset_module("fate"),
            rng=random.Random(0),
        )
