"""RED-phase tests for Story 75-4 — typed ``EntityStore`` generalization.

Generalizes the live ``LoreStore`` machinery (add / query / cosine similarity /
embedding-worker contract) to a universal index typed by ``entity_type``
(ADR-118 §D3). Imports from ``sidequest.game.entity_store`` and
``sidequest.game.entity_card``, which do not exist yet — RED is a clean
``ModuleNotFoundError`` until Dev (GREEN) creates them.

Includes the project-mandated wiring test (server CLAUDE.md: *Every Test Suite
Needs a Wiring Test*) — a fixture-driven behavior test that projects real
entities and queries them through the store, NOT a source-text grep
(server CLAUDE.md: *No Source-Text Wiring Tests*).
"""

from __future__ import annotations

import pytest

from sidequest.game.lore_store import cosine_similarity

from sidequest.game.entity_card import (  # noqa: E402  (module under construction)
    EntityCard,
    EntityType,
    project_faction_card,
    project_location_card,
    project_npc_card,
)
from sidequest.game.entity_store import (  # noqa: E402
    DuplicateEntityId,
    EntityStore,
)
from sidequest.game.npc_pool import NpcPoolMember  # noqa: E402
from sidequest.genre.models.lore import Faction  # noqa: E402


def _card(entity_type: str, entity_id: str, content: str = "some content") -> EntityCard:
    return EntityCard.new(entity_type, entity_id, content=content)


# ---------------------------------------------------------------------------
# AC-3 — typed store: add + query by type
# ---------------------------------------------------------------------------


class TestEntityStoreAddAndType:
    def test_add_then_query_by_type_filters(self) -> None:
        store = EntityStore()
        store.add(_card(EntityType.NPC, "borin"))
        store.add(_card(EntityType.NPC, "skarl"))
        store.add(_card(EntityType.FACTION, "tide_syndicate"))
        store.add(_card(EntityType.LOCATION, "black_hart"))

        npcs = store.query_by_type(EntityType.NPC)
        assert {c.id for c in npcs} == {"npc:borin", "npc:skarl"}

        factions = store.query_by_type(EntityType.FACTION)
        assert [c.id for c in factions] == ["faction:tide_syndicate"]

    def test_query_by_type_empty_for_absent_type(self) -> None:
        store = EntityStore()
        store.add(_card(EntityType.NPC, "borin"))
        assert store.query_by_type(EntityType.FACTION) == []

    def test_duplicate_id_raises(self) -> None:
        """Mirror ``DuplicateLoreId`` — no silent overwrite of an existing card."""
        store = EntityStore()
        store.add(_card(EntityType.NPC, "borin"))
        with pytest.raises(DuplicateEntityId):
            store.add(_card(EntityType.NPC, "borin"))

    def test_len_and_total_tokens(self) -> None:
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="x" * 40))
        store.add(EntityCard.new(EntityType.NPC, "skarl", content="x" * 40))
        assert len(store) == 2
        assert store.total_tokens == 20  # 10 + 10


# ---------------------------------------------------------------------------
# AC-3 / AC-5 — similarity query reuses the live cosine machinery
# ---------------------------------------------------------------------------


class TestEntityStoreSimilarity:
    def test_similarity_ranks_by_cosine(self) -> None:
        """AC-3: reuse ``cosine_similarity`` ranking. The card whose embedding
        points the same way as the query ranks first."""
        store = EntityStore()
        near = EntityCard.new(EntityType.NPC, "near", content="near card")
        far = EntityCard.new(EntityType.NPC, "far", content="far card")
        store.add(near)
        store.add(far)
        store.update_embedding("npc:near", [1.0, 0.0, 0.0])
        store.update_embedding("npc:far", [0.0, 1.0, 0.0])

        ranked = store.query_by_similarity([1.0, 0.0, 0.0], top_k=2)
        assert ranked[0][1].id == "npc:near"
        # the ordering must agree with the shared cosine function
        assert ranked[0][0] == pytest.approx(
            cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0])
        )

    def test_similarity_can_filter_by_type(self) -> None:
        """AC-3: a type-scoped semantic query (cf. ``query_by_category``)."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))
        store.add(EntityCard.new(EntityType.FACTION, "guild", content="smiths guild"))
        store.update_embedding("npc:borin", [1.0, 0.0])
        store.update_embedding("faction:guild", [1.0, 0.0])

        ranked = store.query_by_similarity([1.0, 0.0], top_k=5, entity_type=EntityType.NPC)
        assert [c.id for _, c in ranked] == ["npc:borin"]

    def test_cards_without_embedding_skipped(self) -> None:
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))
        # no embedding assigned → pending, skipped by similarity
        assert store.query_by_similarity([1.0, 0.0], top_k=5) == []


# ---------------------------------------------------------------------------
# AC-5 — embedding-worker contract (drain pending, clear on write-back)
# ---------------------------------------------------------------------------


class TestEntityStoreEmbeddingContract:
    def test_pending_ids_lists_unembedded_cards(self) -> None:
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))
        store.add(EntityCard.new(EntityType.NPC, "skarl", content="enforcer"))
        assert set(store.pending_embedding_ids()) == {"npc:borin", "npc:skarl"}

    def test_update_embedding_clears_pending(self) -> None:
        """AC-5: write-back mirrors ``LoreStore.update_embedding`` — clears the
        pending flag and resets the retry count."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))
        store.update_embedding("npc:borin", [0.1, 0.2, 0.3])
        assert store.pending_embedding_ids() == []
        card = store.query_by_type(EntityType.NPC)[0]
        assert card.embedding == [0.1, 0.2, 0.3]
        assert card.embedding_pending is False

    def test_save_load_round_trip_preserves_embedding(self) -> None:
        """AC-5: like ``LoreStore``, the store serializes embeddings so a
        replayed session queries immediately without re-embedding."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))
        store.update_embedding("npc:borin", [0.4, 0.5])

        restored = EntityStore.model_validate_json(store.model_dump_json())
        card = restored.query_by_type(EntityType.NPC)[0]
        assert card.embedding == [0.4, 0.5]
        assert card.embedding_pending is False


# ---------------------------------------------------------------------------
# AC-7 — OTEL attribute names DEFINED (not emitted; 75-5/75-7 emit)
# ---------------------------------------------------------------------------


class TestOtelAttributeDefinitions:
    def test_card_sync_span_attribute_names_defined(self) -> None:
        """AC-7 / ADR-118 D5: the card-sync span attribute NAMES exist as
        constants now so 75-6/75-7 emit consistent strings. Definitions only —
        no emission asserted (that would overreach into 75-7)."""
        from sidequest.game import entity_card as ec

        assert ec.SPAN_CARD_REPROJECT_COUNT == "card_reproject_count"
        assert ec.SPAN_STALE_CARD_COUNT == "stale_card_count"

    def test_universal_retrieval_attribute_names_defined(self) -> None:
        """AC-7 / ADR-118 D5: the ``retrieval.universal`` per-type count
        attribute names are pinned for 75-5/75-7."""
        from sidequest.game import entity_card as ec

        assert "retrieval.npc_count" in ec.UNIVERSAL_RETRIEVAL_SPAN_ATTRS
        assert "retrieval.location_count" in ec.UNIVERSAL_RETRIEVAL_SPAN_ATTRS
        assert "retrieval.faction_count" in ec.UNIVERSAL_RETRIEVAL_SPAN_ATTRS


# ---------------------------------------------------------------------------
# AC-6 — WIRING TEST (fixture-driven behavior, NOT source-text grep)
# ---------------------------------------------------------------------------


class TestUniversalIndexWiring:
    """Server CLAUDE.md *Every Test Suite Needs a Wiring Test*: prove the store
    is reachable and usable from a realistic construction path — synthesize a
    scenario, project real entities through the real projectors, index them, and
    retrieve. This is behavior, not an implementation-shape assertion."""

    def test_scenario_entities_project_index_and_retrieve(self) -> None:
        # --- synthesize a realistic cast + geography + factions ---
        members = [
            NpcPoolMember(name="Borin", role="blacksmith", drawn_from="world_authored"),
            NpcPoolMember(name="Skarl", role="enforcer", drawn_from="world_authored"),
        ]
        factions = [
            Faction(
                name="Tide Syndicate",
                summary="Smuggling cartel of the wet docks.",
                description="Dockworkers turned smugglers.",
                disposition="hostile",
            )
        ]

        store = EntityStore()
        for m in members:
            store.add(project_npc_card(m))
        for f in factions:
            store.add(project_faction_card(f))
        store.add(
            project_location_card(
                location_id="black_hart",
                name="The Black Hart",
                description="A dockside tavern.",
                linked_npcs=["Borin"],
            )
        )

        # --- the index holds all three entity types, retrievable by type ---
        assert len(store) == 4
        assert {c.id for c in store.query_by_type(EntityType.NPC)} == {
            "npc:borin",
            "npc:skarl",
        }
        assert [c.id for c in store.query_by_type(EntityType.FACTION)] == [
            "faction:tide_syndicate"
        ]
        assert [c.id for c in store.query_by_type(EntityType.LOCATION)] == [
            "loc:black_hart"
        ]

        # --- embedding-worker drain reaches every projected card ---
        assert set(store.pending_embedding_ids()) == {
            "npc:borin",
            "npc:skarl",
            "faction:tide_syndicate",
            "loc:black_hart",
        }

        # --- after the worker writes back, similarity retrieval is live ---
        store.update_embedding("npc:borin", [1.0, 0.0])
        store.update_embedding("npc:skarl", [0.0, 1.0])
        ranked = store.query_by_similarity(
            [1.0, 0.0], top_k=1, entity_type=EntityType.NPC
        )
        assert ranked[0][1].id == "npc:borin"
