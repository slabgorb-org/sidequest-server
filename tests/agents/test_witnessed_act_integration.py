"""witnessed_act end-to-end through run_dispatch_bank (Plan 2, Task 7).

The subsystem's wiring test (project rule): drives the PRODUCTION
run_dispatch_bank path with a hand-built witnessed_act dispatch on a hydrated
Oz-shaped snapshot, proving the subsystem is registered, reachable, gated
(confidence>=threshold engages), and mutates the live dials + records the
ledger end-to-end. (The LLM router emitting the dispatch is Plan 2b; here we
hand-build the package to keep the test deterministic.)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.agents.subsystems import run_dispatch_bank
from sidequest.game.belief_state import BeliefState
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.political_state import PoliticalState
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.models.premises import (
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
)


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


def _pack():
    humbug = PremiseDef(
        premise_id="humbug", authority="the_wizard",
        claim=PremiseClaim(subject="the_wizard", proposition="great and terrible"),
        belief_reserve=90, propped_by=["munchkins"],
        drained_by=[PremiseDrain(act="expose", belief_delta=40)],
        collapse=PremiseCollapse(threshold=20, outcome="He flees."),
    )
    munchkins = BlocDef(
        bloc_id="munchkins", defiance=5, grants_belief_to=["humbug"],
        awakening_acts=[], tipping_threshold=70, tipped_outcome="Revolt.",
    )
    return SimpleNamespace(
        worlds={"oz": SimpleNamespace(premises=[humbug], blocs=[munchkins])},
        # rules is read by run_dispatch_bank's _threshold_for; empty dict → 0.6
        # default, which confidence=0.9 clears so the dispatch engages.
        rules=SimpleNamespace(dispatch_confidence_thresholds={}),
    )


def _snapshot():
    snap = GameSnapshot(world_slug="oz")
    snap.political_state = PoliticalState.from_world(_pack().worlds["oz"])
    snap.npcs = [_make_npc("Dorothy")]
    return snap


def _package():
    return DispatchPackage(
        turn_id="wa-int-turn",
        per_player=[
            PlayerDispatch(
                player_id="Dorothy",
                raw_action="I pull back the curtain and show everyone the little man.",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="witnessed_act",
                        params={"act_id": "expose", "witnesses": ["Dorothy"]},
                        idempotency_key="wa-int-1",
                        visibility={"visible_to": "all"},
                        confidence=0.9,
                    )
                ],
            )
        ],
        cross_player=[],
        confidence_global=0.9,
    )


@pytest.mark.asyncio
async def test_witnessed_act_engages_through_the_bank():
    snap = _snapshot()
    pack = _pack()
    result = await run_dispatch_bank(
        _package(),
        context={
            "snapshot": snap,
            "pack": pack,
            "player_name": "Dorothy",
            "npcs": snap.npcs,
        },
    )
    # The engine actually moved the authoritative dials via the production path.
    assert snap.political_state.premises["humbug"].belief_reserve == 50
    assert snap.political_state.blocs["munchkins"].defiance == 25  # 5 + floor(40*0.5)
    # The ledger kept the receipt.
    assert any(le.effect == "drained" for le in snap.political_state.ledger)
    # ENGAGED, not degraded-to-hint: the handler ran and its output is recorded
    # under the dispatch's idempotency_key (a hint path would have NO such entry).
    assert "wa-int-1" in result.outputs_by_key
    assert result.outputs_by_key["wa-int-1"].data["act_id"] == "expose"
    # The engaged subsystem emits a must_narrate directive (not just "something").
    assert result.directives and result.directives[0].kind == "must_narrate"
