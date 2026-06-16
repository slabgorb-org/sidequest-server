"""Story 84-5 (WI-2) — dormant quest/trope surfaces in retrieval (RED phase).

ADR-118 §A2: a DORMANT quest/trope is recall-by-pertinence — it surfaces when the
player references it ("what happened with the smuggler quest?"). ``retrieve_turn_context``
must return it in new ``retrieved_quests``/``retrieved_tropes`` fields, ``None`` when
none retrieved (zero-byte-leak, mirroring the other typed buckets).

Run ``-n0`` (retrieval span). Synthetic fixtures; net-new symbols imported inside.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.entity_store import EntityStore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager

_VEC = [1.0, 0.0, 0.0]


def _snap(*, current_turn: int) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=current_turn),
        npcs=[],
        npc_pool=[],
    )


class _FakeDaemon:
    def is_available(self) -> bool:
        return True

    async def embed(self, text: str) -> dict[str, Any]:
        return {"embedding": list(_VEC), "model": "fake", "latency_ms": 1}


def _seed(store: EntityStore, entity_type: str, entity_id: str, content: str) -> EntityCard:
    card = EntityCard.new(entity_type, entity_id, content=content)
    store.add(card)
    store.update_embedding(card.id, list(_VEC))
    return card


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ===========================================================================
# AC-6 — new result fields exist
# ===========================================================================


class TestRetrievedQuestTropeFields:
    def test_retrieved_quests_trades_fields_exist(self) -> None:
        from sidequest.game.retrieval_orchestration import RetrievedEntities

        names = {f.name for f in dataclasses.fields(RetrievedEntities)}
        assert "retrieved_quests" in names, "RetrievedEntities must expose retrieved_quests"
        assert "retrieved_tropes" in names, "RetrievedEntities must expose retrieved_tropes"


# ===========================================================================
# AC-6 — dormant quest/trope surfaces by mention/similarity
# ===========================================================================


class TestDormantSurfacesInRetrieval:
    def test_dormant_quest_surfaces_in_retrieval(self) -> None:
        """A thin action (embed runs) over an index holding a dormant quest card
        surfaces it in ``retrieved_quests`` — the recall path."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        store = EntityStore()
        _seed(store, EntityType.QUEST, "q_smuggler", "The Smuggler's Debt — recover the ledger")
        result = _run(
            retrieve_turn_context(
                store,
                _snap(current_turn=10),
                "what ever happened with the smuggler?",
                current_turn=10,
                player_referenced_npcs=set(),
                client=_FakeDaemon(),
            )
        )
        assert result.outcome == "success"
        assert result.retrieved_quests is not None, (
            "a retrieved dormant quest card must surface in retrieved_quests"
        )
        assert "quest:q_smuggler" in {c.id for c in result.retrieved_quests}

    def test_dormant_trope_surfaces_in_retrieval(self) -> None:
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        store = EntityStore()
        _seed(store, EntityType.TROPE, "redemption", "Redemption Arc — a second chance")
        result = _run(
            retrieve_turn_context(
                store,
                _snap(current_turn=10),
                "remind me about the redemption thread",
                current_turn=10,
                player_referenced_npcs=set(),
                client=_FakeDaemon(),
            )
        )
        assert result.retrieved_tropes is not None
        assert "trope:redemption" in {c.id for c in result.retrieved_tropes}

    def test_no_dormant_quest_yields_none(self) -> None:
        """Zero-byte-leak: no quest card retrieved → ``retrieved_quests`` is None."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        store = EntityStore()
        # Only a location card — no quest/trope cards.
        _seed(store, EntityType.LOCATION, "black_hart", "The Black Hart tavern")
        result = _run(
            retrieve_turn_context(
                store,
                _snap(current_turn=10),
                "I look around the tavern",
                current_turn=10,
                player_referenced_npcs=set(),
                client=_FakeDaemon(),
            )
        )
        assert result.retrieved_quests is None
        assert result.retrieved_tropes is None
