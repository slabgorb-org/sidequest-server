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
