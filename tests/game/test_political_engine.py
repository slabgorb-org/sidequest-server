"""apply_witnessed_act engine — drain, couple, awaken, thresholds (Plan 2, Task 3)."""

from __future__ import annotations

from sidequest.genre.models.premises import (
    BlocAwakening,
    BlocDef,
    PremiseClaim,
    PremiseCollapse,
    PremiseDef,
    PremiseDrain,
)
from sidequest.game.political_engine import COUPLING_FRACTION, apply_witnessed_act
from sidequest.game.political_state import BlocState, PoliticalState, PremiseState


def _humbug():
    return PremiseDef(
        premise_id="humbug",
        authority="the_wizard",
        claim=PremiseClaim(subject="the_wizard", proposition="great and terrible"),
        belief_reserve=90,
        propped_by=["munchkins"],
        drained_by=[PremiseDrain(act="expose", belief_delta=40)],
        collapse=PremiseCollapse(threshold=20, outcome="He flees."),
    )


def _munchkins(defiance=0, awaken_delta=10):
    return BlocDef(
        bloc_id="munchkins",
        defiance=defiance,
        grants_belief_to=["humbug"],
        awakening_acts=[BlocAwakening(act="rally", defiance_delta=awaken_delta)],
        tipping_threshold=70,
        tipped_outcome="They revolt.",
    )


def _state(reserve=90, defiance=0):
    return PoliticalState(
        premises={"humbug": PremiseState(premise_id="humbug", belief_reserve=reserve)},
        blocs={"munchkins": BlocState(bloc_id="munchkins", defiance=defiance)},
        ledger=[],
    )


def test_drain_reduces_belief_and_soft_couples_defiance():
    state = _state(reserve=90, defiance=0)
    events = apply_witnessed_act(
        state=state,
        premises=[_humbug()],
        blocs=[_munchkins()],
        act_id="expose",
        witnesses=["Dorothy"],
        turn=1,
    )
    assert state.premises["humbug"].belief_reserve == 50  # 90 - 40
    # soft coupling: floor(40 * COUPLING_FRACTION) into the propping bloc
    assert state.blocs["munchkins"].defiance == int(40 * COUPLING_FRACTION)
    effects = {e.effect for e in events}
    assert "drained" in effects and "coupled" in effects
    # ledger keeps the receipt with the witness
    assert any(le.effect == "drained" and le.witnesses == ["Dorothy"] for le in state.ledger)


def test_awakening_act_raises_defiance_directly():
    state = _state(defiance=0)
    apply_witnessed_act(
        state=state, premises=[_humbug()], blocs=[_munchkins(awaken_delta=15)],
        act_id="rally", witnesses=["Dorothy"], turn=1,
    )
    assert state.blocs["munchkins"].defiance == 15  # awakened, no premise drain (act != drained_by)
    assert state.premises["humbug"].belief_reserve == 90


def test_belief_clamps_at_zero_and_collapse_fires_once():
    state = _state(reserve=30)
    events = apply_witnessed_act(
        state=state, premises=[_humbug()], blocs=[_munchkins()],
        act_id="expose", witnesses=["Dorothy"], turn=1,
    )
    assert state.premises["humbug"].belief_reserve == 0  # 30-40 clamped
    assert state.premises["humbug"].collapsed is True
    collapse_events = [e for e in events if e.effect == "collapsed"]
    assert len(collapse_events) == 1
    assert collapse_events[0].detail == "He flees."
    # collapsed premise does not drain again
    again = apply_witnessed_act(
        state=state, premises=[_humbug()], blocs=[_munchkins()],
        act_id="expose", witnesses=["Dorothy"], turn=2,
    )
    assert not any(e.effect == "drained" for e in again)


def test_bloc_tips_when_defiance_crosses_threshold():
    state = _state(defiance=60)
    events = apply_witnessed_act(
        state=state, premises=[_humbug()], blocs=[_munchkins(defiance=60, awaken_delta=15)],
        act_id="rally", witnesses=["Dorothy"], turn=1,
    )
    assert state.blocs["munchkins"].defiance == 75
    assert state.blocs["munchkins"].tipped is True
    tip = [e for e in events if e.effect == "tipped"]
    assert len(tip) == 1 and tip[0].detail == "They revolt."


def test_unmatched_act_is_a_noop():
    state = _state()
    events = apply_witnessed_act(
        state=state, premises=[_humbug()], blocs=[_munchkins()],
        act_id="not_an_act", witnesses=["Dorothy"], turn=1,
    )
    assert events == []
    assert state.premises["humbug"].belief_reserve == 90
    assert state.ledger == []


def test_coupling_can_tip_a_propping_bloc():
    # Soft coupling alone can push a bloc over the line — caught by the final pass.
    state = _state(reserve=90, defiance=69)
    apply_witnessed_act(
        state=state, premises=[_humbug()], blocs=[_munchkins(defiance=69)],
        act_id="expose", witnesses=["Dorothy"], turn=1,
    )
    # coupled += floor(40*0.5)=20 → 89 ≥ 70
    assert state.blocs["munchkins"].tipped is True


# --- 59-28: touched-this-turn guard on the collapse/tip threshold pass ---------
#
# The final threshold pass must only fire collapse/tip for dials this act actually
# moved (or pushed across the line). A dial sitting at/over threshold AT REST, that
# this turn's act never touches, must NOT fire — otherwise a world that authors a
# bloc at/above its tipping_threshold tips on the first unrelated witnessed act.


def _winkies(defiance=70):
    # A second bloc NOT propped by humbug (so coupling never reaches it) and awakened
    # only by an unrelated act — the at-rest, untouched control bloc.
    return BlocDef(
        bloc_id="winkies",
        defiance=defiance,
        grants_belief_to=[],
        awakening_acts=[BlocAwakening(act="storm_castle", defiance_delta=10)],
        tipping_threshold=70,
        tipped_outcome="The Winkies rise.",
    )


def test_unrelated_act_does_not_tip_bloc_already_at_threshold():
    # Bloc pre-seeded AT its tipping_threshold (70 ≥ 70) at rest. An act that does not
    # drain humbug, couple munchkins, or awaken munchkins must leave it un-tipped.
    state = _state(defiance=70)
    events = apply_witnessed_act(
        state=state, premises=[_humbug()], blocs=[_munchkins(defiance=70)],
        act_id="not_an_act", witnesses=["Dorothy"], turn=1,
    )
    assert not any(e.effect == "tipped" for e in events)
    assert state.blocs["munchkins"].tipped is False
    assert not any(le.effect == "tipped" for le in state.ledger)


def test_unrelated_act_does_not_collapse_premise_already_at_threshold():
    # Premise pre-seeded AT its collapse threshold (20 ≤ 20) at rest. An unrelated act
    # must not collapse it — the dial did not move this turn.
    state = _state(reserve=20)
    events = apply_witnessed_act(
        state=state, premises=[_humbug()], blocs=[_munchkins()],
        act_id="not_an_act", witnesses=["Dorothy"], turn=1,
    )
    assert not any(e.effect == "collapsed" for e in events)
    assert state.premises["humbug"].collapsed is False
    assert not any(le.effect == "collapsed" for le in state.ledger)


def test_touched_bloc_tips_while_at_rest_bloc_does_not_in_same_call():
    # The guard must be act-scoped, not global. In ONE call: munchkins is awakened
    # (60 + 15 = 75, crosses 70 → tips) while winkies sits at 70 at rest, untouched
    # by this act (rally awakens munchkins, not winkies; winkies is unpropped so no
    # coupling reaches it). Only the touched bloc may tip.
    state = PoliticalState(
        premises={"humbug": PremiseState(premise_id="humbug", belief_reserve=90)},
        blocs={
            "munchkins": BlocState(bloc_id="munchkins", defiance=60),
            "winkies": BlocState(bloc_id="winkies", defiance=70),
        },
        ledger=[],
    )
    events = apply_witnessed_act(
        state=state,
        premises=[_humbug()],
        blocs=[_munchkins(defiance=60, awaken_delta=15), _winkies(defiance=70)],
        act_id="rally",
        witnesses=["Dorothy"],
        turn=1,
    )
    # Touched bloc crosses the line → tips.
    assert state.blocs["munchkins"].tipped is True
    # At-rest, untouched bloc must NOT tip even though 70 ≥ 70.
    assert state.blocs["winkies"].tipped is False
    tipped_ids = {e.target_id for e in events if e.effect == "tipped"}
    assert tipped_ids == {"munchkins"}
    assert not any(
        le.effect == "tipped" and le.target_id == "winkies" for le in state.ledger
    )
