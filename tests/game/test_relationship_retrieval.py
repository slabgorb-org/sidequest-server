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


class TestRelationshipCardOnNamedPresentPath:
    """§A2: a relationship card surfaces BECAUSE the related NPC is named/present —
    NOT via cosine similarity. The mechanism is FLOOR-COMPANION: a present/named
    NPC's ``rel:<slug>`` card rides the floor alongside its ``npc:<slug>`` card, so
    it reaches ``retrieved_relationships`` on the very turn the player engages the
    NPC — including the drama-gate-skip turn where the cosine fill never runs.

    This is the path the Reviewer's blocker targets: the prior AC-6 test seeded a
    cosine embedding + a thin action (the ONE path that works) and so MASKED the
    dead §A2 path. These tests drive the real named/present intent and currently
    return None (the floor-companion mechanism is unbuilt) — that is the RED.
    """

    def test_named_present_npc_surfaces_relationship_card_on_gate_skip(self) -> None:
        """THE §A2 contract. Borin is scene-present AND player-referenced, so the
        84-1 drama-gate SKIPS the cosine embed (``embed_skipped=True``). On that
        same turn the relationship card for Borin must still surface in
        ``retrieved_relationships`` — it rides the floor because Borin is present,
        not because anything matched a vector. The cosine fill is empty here; the
        rel card must NOT depend on it."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        store = EntityStore()
        # Borin's relationship card is indexed (projected by entity_sync), with a
        # ZERO vector so it can NEVER win a cosine match — proving the surfacing is
        # structural (floor), not similarity.
        rel = EntityCard.new(
            EntityType.RELATIONSHIP, "borin", content="Borin — Warm — saved the party"
        )
        store.add(rel)
        store.update_embedding(rel.id, [0.0, 0.0, 0.0])

        snap = _snap(current_turn=10, npcs=[_npc("Borin", 10)])  # scene-present

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I attack Borin",  # named + present → drama-gate skips the embed
                current_turn=10,
                player_referenced_npcs={"Borin"},
                client=_FakeDaemon(),
            )
        )

        # The drama-gate genuinely skipped the cosine pass this turn.
        assert result.embed_skipped is True, (
            "a named+present action must trigger the 84-1 drama-gate skip"
        )
        # ...and YET the relationship card surfaced — via the floor, not cosine.
        assert result.retrieved_relationships is not None, (
            "§A2: a present/named NPC's relationship card MUST surface even when "
            "the cosine embed is skipped — it rides the floor (Reviewer blocker)"
        )
        assert "rel:borin" in {c.id for c in result.retrieved_relationships}

    def test_present_npc_relationship_card_does_not_need_cosine(self) -> None:
        """A scene-present NPC (not explicitly named) still gets its relationship
        card surfaced — present-scene is a structural signal. A zero-vector rel
        card (no cosine match possible) must still ride the floor."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        store = EntityStore()
        rel = EntityCard.new(EntityType.RELATIONSHIP, "borin", content="Borin — Warm — ally")
        store.add(rel)
        store.update_embedding(rel.id, [0.0, 0.0, 0.0])  # never wins cosine

        snap = _snap(current_turn=10, npcs=[_npc("Borin", 10)])  # present floor

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I look around the room",  # nobody named; Borin is just present
                current_turn=10,
                player_referenced_npcs=set(),
                client=_FakeDaemon(),
            )
        )
        assert result.retrieved_relationships is not None, (
            "a scene-present NPC's relationship card must surface via the floor "
            "(structural presence), not require a cosine hit"
        )
        assert "rel:borin" in {c.id for c in result.retrieved_relationships}

    def test_absent_npc_relationship_card_not_surfaced(self) -> None:
        """The complement: an NPC who is NEITHER present NOR named does not pull
        its relationship card onto the floor. With no present/named NPC and no
        cosine match, ``retrieved_relationships`` is None (zero-byte-leak)."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        store = EntityStore()
        rel = EntityCard.new(EntityType.RELATIONSHIP, "borin", content="Borin — Warm — ally")
        store.add(rel)
        store.update_embedding(rel.id, [0.0, 0.0, 0.0])  # never wins cosine

        # Borin is off-stage (last_seen far in the past) and not named.
        snap = _snap(current_turn=20, npcs=[_npc("Borin", last_seen_turn=2)])

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I wander the empty corridor",
                current_turn=20,
                player_referenced_npcs=set(),
                client=_FakeDaemon(),
            )
        )
        assert result.retrieved_relationships is None, (
            "an absent, unnamed NPC must not pull its relationship card onto the floor"
        )
