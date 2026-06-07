"""witnessed_act: router emission flows through the pass into the engine (Plan 2b, Task 6)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
    WitnessedActArchetype,
)
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
)
from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass


def _humbug() -> PremiseDef:
    return PremiseDef(
        premise_id="the_wizards_humbug",
        authority="oz_the_great",
        claim=PremiseClaim(subject="oz_the_great", proposition="great and terrible"),
        belief_reserve=90,
        propped_by=["munchkins"],
        drained_by=[PremiseDrain(act="expose_the_humbug", belief_delta=40)],
        collapse=PremiseCollapse(threshold=20, outcome="He flees in his balloon."),
    )


def _munchkins() -> BlocDef:
    return BlocDef(
        bloc_id="munchkins",
        defiance=5,
        grants_belief_to=["the_wizards_humbug"],
        awakening_acts=[BlocAwakening(act="organize_a_first_small_refusal", defiance_delta=10)],
        tipping_threshold=70,
        tipped_outcome="The Munchkins revolt.",
    )


def _oz_world():
    return SimpleNamespace(premises=[_humbug()], blocs=[_munchkins()])


def _oz_pack():
    return SimpleNamespace(
        worlds={"oz": _oz_world()},
        witnessed_acts=[
            WitnessedActArchetype(id="expose_the_humbug", label="Expose the Humbug", description="x"),
        ],
        rules=None,
    )


def _npc(name: str, *, location: str) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name, description="A Munchkin villager.", personality="Hopeful.",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        belief_state=BeliefState(),
        location=location,
    )


def _oz_snapshot() -> GameSnapshot:
    snap = GameSnapshot(world_slug="oz")
    snap.genre_slug = "wry_whimsy"
    snap.player_seats = {"seat-1": "Dorothy"}
    snap.character_locations = {"Dorothy": "munchkin_country"}
    snap.npcs = [_npc("Boq", location="munchkin_country")]
    snap.political_state = PoliticalState.from_world(_oz_world())
    return snap


class _StubRouter:
    def __init__(self, package):
        self._package = package
        self.seen_summary = None

    async def decompose(self, *, action, state_summary):
        self.seen_summary = state_summary
        return self._package


def _package() -> DispatchPackage:
    return DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="Dorothy",
                raw_action="I pull the green curtain aside in front of Boq",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="witnessed_act",
                        params={"act_id": "expose_the_humbug", "witnesses": ["Boq"]},
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
async def test_router_emitted_witnessed_act_engages_the_engine():
    snap = _oz_snapshot()
    router = _StubRouter(_package())

    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=_oz_pack(),
        action="I pull the green curtain aside in front of Boq",
        player_name="Dorothy",
    )

    # The router received the surfaced vocabulary + witness candidate set.
    assert "witnessed_act_vocabulary" in router.seen_summary
    assert router.seen_summary["present_npcs"] == ["Boq"]

    # The engine actually moved the authoritative dials via the production path.
    assert snap.political_state.premises["the_wizards_humbug"].belief_reserve == 50  # 90 - 40
    assert snap.political_state.blocs["munchkins"].defiance == 25  # 5 + floor(40*0.5)

    # The ledger kept the receipt naming the witness.
    drained = [le for le in snap.political_state.ledger if le.effect == "drained"]
    assert drained and drained[0].witnesses == ["Boq"]

    # ADR-053 reuse: the witness received a contradicting belief.
    assert len(snap.npcs[0].belief_state.beliefs) == 1
