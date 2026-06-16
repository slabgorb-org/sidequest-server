"""witnessed_act dispatch subsystem (Plan 2, Task 6)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.agents.subsystems import get_registered
from sidequest.agents.subsystems.witnessed_act import run_witnessed_act_dispatch
from sidequest.game.belief_state import BeliefState
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.political_state import PoliticalState
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.models.premises import (
    BlocAwakening,
    BlocDef,
    PremiseClaim,
    PremiseCollapse,
    PremiseDef,
    PremiseDrain,
)
from sidequest.protocol.dispatch import SubsystemDispatch


def _make_npc(name: str) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="A traveller.",
            personality="Curious and brave.",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        belief_state=BeliefState(),
    )


def _dispatch(params):
    return SubsystemDispatch(
        subsystem="witnessed_act",
        params=params,
        idempotency_key="wa-1",
        visibility={"visible_to": "all"},
        confidence=0.9,
    )


def _pack():
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
    return SimpleNamespace(worlds={"oz": SimpleNamespace(premises=[humbug], blocs=[munchkins])})


def _snapshot():
    snap = GameSnapshot(world_slug="oz")
    snap.political_state = PoliticalState.from_world(_pack().worlds["oz"])
    snap.npcs = [_make_npc("Dorothy")]
    snap.turn_manager.interaction = 7
    return snap


def test_registered_in_bank():
    assert "witnessed_act" in get_registered()


# NOTE: the witnessed_act precondition (political_state-None → inert) is covered
# at the gate level in tests/agents/test_dispatch_precondition_gate.py (Story
# 59-29 co-location), alongside the sibling scenario_clue precondition tests.


@pytest.mark.asyncio
async def test_handler_drains_injects_and_returns_directive():
    snap = _snapshot()
    out = await run_witnessed_act_dispatch(
        _dispatch({"act_id": "expose", "witnesses": ["Dorothy"]}),
        snapshot=snap,
        pack=_pack(),
        player_name="Dorothy",
        npcs=snap.npcs,
    )
    assert snap.political_state.premises["humbug"].belief_reserve == 50
    assert snap.political_state.blocs["munchkins"].defiance == 25  # 5 + floor(40*0.5)
    # ADR-053 reuse: the witness got a contradicting fact
    assert len(snap.npcs[0].belief_state.beliefs) == 1
    # turn propagation: turn_manager.interaction flows to the injected belief
    assert snap.npcs[0].belief_state.beliefs[0].turn_learned == 7
    # ...and into the political ledger
    assert any(le.turn == 7 for le in snap.political_state.ledger)
    # narrator gets told something mechanical happened
    assert out.directives and out.directives[0].kind == "must_narrate"
    assert out.data["act_id"] == "expose"


@pytest.mark.asyncio
async def test_no_witness_moves_nothing():
    snap = _snapshot()
    out = await run_witnessed_act_dispatch(
        _dispatch({"act_id": "expose", "witnesses": []}),
        snapshot=snap,
        pack=_pack(),
        player_name="Dorothy",
        npcs=snap.npcs,
    )
    assert snap.political_state.premises["humbug"].belief_reserve == 90  # unmoved
    assert out.data.get("error") == "no_witness"


@pytest.mark.asyncio
async def test_missing_act_id_raises():
    snap = _snapshot()
    with pytest.raises(ValueError, match="act_id"):
        await run_witnessed_act_dispatch(
            _dispatch({"witnesses": ["Dorothy"]}),
            snapshot=snap,
            pack=_pack(),
            player_name="Dorothy",
            npcs=snap.npcs,
        )
