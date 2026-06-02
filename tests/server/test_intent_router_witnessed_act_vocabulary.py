"""witnessed_act state-summary projections + present-NPC helper (Plan 2b, Tasks 2-3)."""

from __future__ import annotations

from sidequest.game.belief_state import BeliefState
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.political_state import PoliticalState
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.models.premises import WitnessedActArchetype
from sidequest.server.intent_router_pass import _present_npc_names


def _npc(name: str, *, location: str | None = None) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="A villager of the Munchkin country.",
            personality="Wary but hopeful.",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        belief_state=BeliefState(),
        location=location,
    )


def _oz_snapshot() -> GameSnapshot:
    """A hydrated political snapshot whose party has a consensus location."""
    snap = GameSnapshot(world_slug="oz")
    snap.genre_slug = "wry_whimsy"
    snap.player_seats = {"seat-1": "Dorothy"}
    snap.character_locations = {"Dorothy": "munchkin_country"}
    snap.npcs = [
        _npc("Boq", location="munchkin_country"),     # present
        _npc("Glinda", location="quadling_country"),  # elsewhere
    ]
    snap.political_state = PoliticalState(
        premises={}, blocs={}, ledger=[]
    )
    return snap


def _acts() -> list[WitnessedActArchetype]:
    return [
        WitnessedActArchetype(id="expose_the_humbug", label="Expose the Humbug", description="Pull the curtain."),
        WitnessedActArchetype(id="refuse_the_premise", label="Refuse the Premise", description="Decline the rule."),
    ]


def test_present_npc_names_returns_only_in_scene_npcs():
    snap = _oz_snapshot()
    names = _present_npc_names(snap)
    assert names == ["Boq"]  # Glinda is in quadling_country, not present


def test_present_npc_names_empty_when_party_location_unresolved():
    # No seated PCs → party_location() is None → no one is "present".
    snap = _oz_snapshot()
    snap.player_seats = {}
    assert _present_npc_names(snap) == []
