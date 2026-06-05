"""Story 84-3 (WI-4) — relationship card surfaces in retrieval (RED phase).

ADR-118 §A2: a relationship card is index-side and surfaces because the related
NPC is named/present. ``retrieve_turn_context`` must return it in a new
``retrieved_relationships`` field (mirroring ``retrieved_npcs/locations/factions``),
``None`` when none retrieved (zero-byte-leak).

Run ``-n0`` (retrieval span). Synthetic fixtures; net-new symbols imported inside
each test.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from sidequest.game.creature_core import CreatureCore
from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.entity_store import EntityStore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager

_VEC = [1.0, 0.0, 0.0]


def _npc(name: str, last_seen_turn: int) -> Npc:
    return Npc(
        core=CreatureCore(name=name, description=f"{name} is here.", personality="stoic"),
        last_seen_turn=last_seen_turn,
    )


def _snap(*, current_turn: int, npcs: list[Npc] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=current_turn),
        npcs=npcs or [],
        npc_pool=[],
    )


class _FakeDaemon:
    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = vector or _VEC

    def is_available(self) -> bool:
        return True

    async def embed(self, text: str) -> dict[str, Any]:
        return {"embedding": list(self._vector), "model": "fake", "latency_ms": 1}


def _seed_relationship_card(store: EntityStore, entity_id: str, content: str) -> EntityCard:
    """Seed an embedded RELATIONSHIP card so the fill can select it deterministically."""
    card = EntityCard.new(EntityType.RELATIONSHIP, entity_id, content=content)
    store.add(card)
    store.update_embedding(card.id, list(_VEC))
    return card


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ===========================================================================
# AC-6 — retrieved_relationships field exists + surfaces in retrieval
# ===========================================================================


class TestRetrievedRelationshipsField:
    def test_retrieved_relationships_field_exists(self) -> None:
        """``RetrievedEntities`` must carry a ``retrieved_relationships`` field so
        relationship cards have a typed home in the result (mirrors the other
        ``retrieved_*`` fields)."""
        from sidequest.game.retrieval_orchestration import RetrievedEntities

        names = {f.name for f in dataclasses.fields(RetrievedEntities)}
        assert "retrieved_relationships" in names, (
            "RetrievedEntities must expose retrieved_relationships: list[EntityCard] | None"
        )


class TestRelationshipCardInRetrieval:
    def test_relationship_card_surfaces_in_retrieval(self) -> None:
        """A thin action (embed runs) over an index holding a RELATIONSHIP card
        surfaces it in ``retrieved_relationships`` — the card reaches the fill."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        store = EntityStore()
        _seed_relationship_card(
            store, "borin", "Borin — friendly — saved the party from the ogre"
        )
        snap = _snap(current_turn=10, npcs=[])  # thin action → cosine fill runs

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "tell me about my history with the dwarf",
                current_turn=10,
                player_referenced_npcs=set(),
                client=_FakeDaemon(),
            )
        )

        assert result.outcome == "success"
        assert result.retrieved_relationships is not None, (
            "a retrieved RELATIONSHIP card must surface in retrieved_relationships"
        )
        assert [c.id for c in result.retrieved_relationships] == ["rel:borin"]

    def test_no_relationship_card_yields_none(self) -> None:
        """Zero-byte-leak: with no relationship card retrieved,
        ``retrieved_relationships`` is ``None`` (not an empty list), mirroring the
        other typed fields."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        store = EntityStore()
        # Only a LOCATION card in the index — no relationship cards.
        loc = EntityCard.new(EntityType.LOCATION, "black_hart", content="The Black Hart tavern")
        store.add(loc)
        store.update_embedding(loc.id, list(_VEC))
        snap = _snap(current_turn=10, npcs=[])

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I look around the tavern",
                current_turn=10,
                player_referenced_npcs=set(),
                client=_FakeDaemon(),
            )
        )
        assert result.retrieved_relationships is None, (
            "no relationship card retrieved → retrieved_relationships is None (zero-byte-leak)"
        )
