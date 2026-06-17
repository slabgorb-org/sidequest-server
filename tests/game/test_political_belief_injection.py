"""Witnessed acts inject a contradicting BeliefFact via ADR-053 (Plan 2, Task 4)."""

from __future__ import annotations

from sidequest.game.belief_state import BeliefState
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.political_engine import inject_witnessed_contradiction
from sidequest.game.session import Npc
from sidequest.genre.models.premises import PremiseClaim, PremiseCollapse, PremiseDef


def _make_npc(name: str) -> Npc:
    """Minimal Npc fixture with the given name and an empty BeliefState."""
    core = CreatureCore(
        name=name,
        description="A traveller.",
        personality="Curious and brave.",
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Npc(core=core, belief_state=BeliefState())


def _premise():
    return PremiseDef(
        premise_id="humbug",
        authority="the_wizard",
        claim=PremiseClaim(subject="the_wizard", proposition="great and terrible"),
        belief_reserve=90,
        collapse=PremiseCollapse(threshold=20, outcome="He flees."),
    )


def test_only_witnesses_receive_the_contradicting_fact():
    dorothy = _make_npc("Dorothy")
    bystander = _make_npc("Boq")
    n = inject_witnessed_contradiction(
        npcs=[dorothy, bystander], witnesses=["Dorothy"], premise=_premise(), turn=3
    )
    assert n == 1
    assert len(dorothy.belief_state.beliefs) == 1
    assert len(bystander.belief_state.beliefs) == 0
    fact = dorothy.belief_state.beliefs[0]
    assert fact.variant == "fact"
    assert fact.subject == "the_wizard"
    assert fact.source.kind == "witnessed"
    assert fact.turn_learned == 3


def test_no_witnesses_injects_nothing():
    dorothy = _make_npc("Dorothy")
    n = inject_witnessed_contradiction(npcs=[dorothy], witnesses=[], premise=_premise(), turn=1)
    assert n == 0
    assert dorothy.belief_state.beliefs == []
