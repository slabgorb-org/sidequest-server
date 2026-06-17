"""Story 73-2 → repurposed as the Westley major M1 WIRING TEST (ADR-144 REPLACE).

The ``trial`` confrontation (tea_and_murder) is a **Fate Contest**
(``resolution_mode: contest``). M1's doctrinal finding: a contest that still
advertises dial beats lets the narrator select one and run the legacy dial
``apply_beat`` engine IN PARALLEL to the 4dF Contest engine — the layering ADR-144
forbids. The fix is two-fold:

  (a) server: ``narration_apply`` now has an explicit ``contest`` branch that drops
      any stray ``beat_selection`` on a contest encounter (loudly) and NEVER runs
      the dial ``apply_beat`` engine; ``_gate_applies_to_encounter`` excludes
      contest mode.
  (b) content: the dial beats are stripped from the contest defs.

This file is the missing wiring test for the contest path (the reviewer's M1 ask):
it dispatches a contest encounter through ``narration_apply`` WITH a beat_selection
and asserts the dial ``apply_beat`` is NEVER reached, the encounter is NOT resolved
by the dial, and the contest still resolves via the 4dF FATE_ACTION path.

History: this file once pinned a terminal ``push``/``concede`` beat that resolved a
trial via ``apply_beat`` on any tier (the 59-8/67-10 soft-lock fix). Those dial
beats were exactly the M1 bleed and have been stripped — see the Delivery Finding
on contest voluntary-exit for the follow-up that question raises.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.encounter import (
    ContestState,
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.rules import ResolutionMode
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = (
    Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
)

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)

_CONTEST_CONFRONTATIONS = ["trial", "social_duel", "negotiation", "scandal"]


def _pack():
    return load_genre_pack(CONTENT_GENRE_PACKS / "tea_and_murder")


def _cdef(ctype: str):
    cdef = find_confrontation_def(_pack().rules.confrontations, ctype)
    assert cdef is not None, f"tea_and_murder must define a {ctype} confrontation"
    return cdef


_DIAL_FIELDS = ("kind", "stat_check", "deltas", "target_tag", "resolution")


@pytest.mark.parametrize("ctype", _CONTEST_CONFRONTATIONS)
def test_social_confrontation_is_a_contest_with_no_armed_dial_beats(ctype: str):
    """All four tea_and_murder social confrontations are Fate Contests; any beat they
    carry is a display-only stub (no dial-resolution field) — the content half of the
    M1 REPLACE fix (ADR-144). The stub ids feed the world-tier class Abilities tab."""
    cdef = _cdef(ctype)
    assert cdef.resolution_mode == ResolutionMode.contest
    for beat in cdef.beats:
        armed = [f for f in _DIAL_FIELDS if getattr(beat, f, None) is not None]
        if beat.base != 1:
            armed.append("base")
        assert not armed, (
            f"{ctype} contest beat {beat.id!r} carries dial-resolution field(s) "
            f"{armed}; a contest beat must be a display-only stub (ADR-144 REPLACE)"
        )


def _trial_contest_encounter() -> StructuredEncounter:
    enc = StructuredEncounter(
        encounter_type="trial",
        win_condition="dial_threshold",
        category="social",
        player_metric=EncounterMetric(name="conviction", current=0, starting=0, threshold=3),
        opponent_metric=EncounterMetric(name="conviction", current=0, starting=0, threshold=3),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(name="Inspector Pryce", role="participant", side="player"),
            EncounterActor(name="Crown Prosecutor", role="participant", side="opponent"),
        ],
    )
    enc.contest = ContestState(target=3)
    return enc


def test_contest_beat_selection_never_reaches_the_dial_engine(monkeypatch):
    """M1 WIRING TEST (the reviewer's core ask): a stray ``beat_selection`` arriving
    on a contest encounter must be dropped — the legacy dial ``apply_beat`` engine
    must NEVER run on a contest. We tripwire ``apply_beat`` (raises if reached) and
    drive a player beat-selection through the REAL narration-apply path. The dial
    must not fire, and the encounter must NOT be resolved by the dial (it persists
    for the 4dF FATE_ACTION exchange)."""
    import sidequest.game.beat_kinds as beat_kinds_pkg

    def _dial_tripwire(*_a, **_kw):
        raise AssertionError(
            "apply_beat (the dial engine) was reached on a Fate Contest path — the "
            "exact ADR-144 REPLACE violation M1 closes (the dial must never resolve "
            "a contest; FATE_ACTION's 4dF exchange does)"
        )

    monkeypatch.setattr(beat_kinds_pkg, "apply_beat", _dial_tripwire)

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.encounter = _trial_contest_encounter()

    # A stray narrator beat-selection against the contest encounter. (The narrator is
    # an LLM and could hallucinate one even though the def now carries no beats.)
    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(
                actor="Inspector Pryce",
                beat_id="cross_examine",  # a former dial beat id, now stripped
                outcome=RollOutcome.Success,
            ),
        ],
    )

    # No exception => the dial tripwire was never tripped.
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Inspector Pryce",
        pack=_pack(),
        from_explicit_action=False,
        room=room_for(snap),
    )

    assert snap.encounter is not None, "the contest encounter must persist, not be dropped"
    assert snap.encounter.resolved is False, (
        "the dial must NOT resolve a contest — the stray beat is dropped and the "
        "encounter stays live for the 4dF FATE_ACTION exchange (ADR-144 REPLACE)"
    )
    # The dial tally is untouched (the dropped beat advanced nothing).
    assert snap.encounter.contest is not None
    assert snap.encounter.contest.player_victories == 0
    assert snap.encounter.contest.opponent_victories == 0


def test_contest_resolves_through_the_fate_action_4df_path():
    """The other half of the M1 ask: with the dial blocked, the contest STILL
    resolves — via the 4dF exchange engine reached through FATE_ACTION. Drive a
    one-PC overcome that closes the barrier and crosses the (pre-seeded) target."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore
    from sidequest.game.fate_sheet import FateSheet
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.session import Npc
    from sidequest.protocol.fate import FateActionPayload
    from sidequest.server.dispatch.fate_conflict import dispatch_fate_action
    from sidequest.server.dispatch.fate_contest import FateContestResult

    enc = _trial_contest_encounter()
    enc.actors[0].name = "Lady Ash"
    enc.actors[1].name = "The Vicar"
    enc.contest = ContestState(target=3, player_victories=2)  # one PC win ends it

    snap = GameSnapshot(
        genre_slug="tea_and_murder",
        characters=[
            Character(
                core=CreatureCore(
                    name="Lady Ash",
                    description="d",
                    personality="p",
                    fate_sheet=FateSheet(skills={"Rapport": 4}),
                ),
                char_class="Agent",
                race="Human",
                backstory="b",
            )
        ],
        npcs=[
            Npc(
                core=CreatureCore(
                    name="The Vicar",
                    description="d",
                    personality="p",
                    fate_sheet=FateSheet(skills={"Rapport": 1}),
                )
            )
        ],
        encounter=enc,
    )

    res = dispatch_fate_action(
        payload=FateActionPayload(request_id="r1", action="overcome", skill="Rapport", difficulty=0),
        actor_name="Lady Ash",
        encounter=enc,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap,
        rng=random.Random(0),
    )
    assert res.commitment_pending is False
    assert isinstance(res.exchange, FateContestResult)
    assert res.exchange.resolved is True, "the contest must resolve via the 4dF path"
    assert enc.resolved is True
    assert enc.outcome == "player_victory"
