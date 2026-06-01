"""RED-phase tests for Story 75-4 — ``EntityCard`` model + per-type projectors.

Foundation slice of the ADR-118 universal retrieval layer. These tests pin the
card model and the NPC / location / faction projectors. They import from
``sidequest.game.entity_card``, which does not exist yet — RED is a clean
``ModuleNotFoundError`` until Dev (GREEN) creates the module.

Contract sources (in spec-authority order):
- ``.session/75-4-session.md`` (canonical technical approach + ACs)
- ``sprint/context/context-story-75-4.md`` (test-strategy lens)
- ADR-118 §D3 (the ``EntityCard`` abstraction, namespaced IDs, projectors)

Reuse discipline (ADR-118 D3, SOUL *Don't Reinvent*): the card is a structural
sibling of ``LoreFragment`` and MUST share the embedding-worker contract and the
``_estimate_tokens`` math — asserted explicitly below.
"""

from __future__ import annotations

import pytest

from sidequest.game.lore_store import _estimate_tokens

# The module under construction (does not exist yet → RED).
from sidequest.game.entity_card import (  # noqa: E402
    EntityCard,
    EntityType,
    project_faction_card,
    project_location_card,
    project_npc_card,
)
from sidequest.game.npc_pool import NpcPoolMember  # noqa: E402
from sidequest.game.disposition import Attitude, Disposition  # noqa: E402
from sidequest.genre.models.lore import Faction  # noqa: E402


# ---------------------------------------------------------------------------
# AC-1 — EntityCard model
# ---------------------------------------------------------------------------


class TestEntityCardModel:
    def test_new_namespaces_id_by_type(self) -> None:
        """AC-1: stable, namespaced ids — ``<type>:<entity_id>`` (ADR-118 D3)."""
        card = EntityCard.new(EntityType.NPC, "borin", content="Borin, a smith.")
        assert card.id == "npc:borin"
        assert card.entity_type == "npc"

    def test_new_computes_token_estimate_from_content(self) -> None:
        """AC-1: token_estimate is computed from content, never caller-supplied,
        and uses the SAME math as LoreFragment (reuse discipline)."""
        content = "x" * 40
        card = EntityCard.new(EntityType.FACTION, "tide_syndicate", content=content)
        assert card.token_estimate == _estimate_tokens(content)
        assert card.token_estimate == 10

    def test_new_defaults_match_embedding_worker_contract(self) -> None:
        """AC-1: identical worker contract to LoreFragment so the existing
        embedding worker drains EntityCards unchanged (ADR-118 D3)."""
        card = EntityCard.new(EntityType.NPC, "borin", content="Borin.")
        assert card.embedding is None
        assert card.embedding_pending is True
        assert card.embedding_retry_count == 0

    def test_entity_ref_defaults_to_entity_id_back_pointer(self) -> None:
        """AC-1: entity_ref is a back-pointer to the system-of-record struct;
        the card owns no truth (ADR-118 D1)."""
        card = EntityCard.new(EntityType.NPC, "borin", content="Borin.")
        assert card.entity_ref == "borin"

    def test_entity_ref_can_be_explicit(self) -> None:
        card = EntityCard.new(
            EntityType.LOCATION,
            "black_hart",
            content="The Black Hart, a tavern.",
            entity_ref="room_graph:black_hart",
        )
        assert card.entity_ref == "room_graph:black_hart"

    def test_blank_content_is_rejected(self) -> None:
        """AC-1 / No Silent Fallbacks: whitespace-only content would embed to a
        degenerate vector — reject at the construction boundary, mirroring
        ``LoreFragment._content_must_not_be_blank``."""
        with pytest.raises(ValueError):
            EntityCard.new(EntityType.NPC, "ghost", content="   ")

    def test_empty_content_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            EntityCard.new(EntityType.NPC, "ghost", content="")

    def test_metadata_round_trips(self) -> None:
        card = EntityCard.new(
            EntityType.NPC,
            "borin",
            content="Borin.",
            metadata={"last_seen_turn": "7", "disposition_tier": "friendly"},
        )
        assert card.metadata["last_seen_turn"] == "7"
        assert card.metadata["disposition_tier"] == "friendly"

    def test_extra_fields_forbidden(self) -> None:
        """Mirror LoreFragment's ``extra='forbid'`` — typo-proof the model."""
        with pytest.raises(Exception):
            EntityCard(
                id="npc:x",
                entity_type="npc",
                entity_ref="x",
                content="x",
                token_estimate=1,
                bogus_field="nope",
            )


# ---------------------------------------------------------------------------
# AC-2 — NPC projector (reads the real NpcPoolMember struct)
# ---------------------------------------------------------------------------


class TestNpcProjector:
    def test_projects_core_identity_fields_into_content(self) -> None:
        """AC-2: NPC card content carries name, role, pronouns (ADR-118 D3)."""
        member = NpcPoolMember(
            name="Borin",
            role="blacksmith",
            pronouns="he/him",
            drawn_from="world_authored",
        )
        card = project_npc_card(member)
        assert card.entity_type == "npc"
        assert "Borin" in card.content
        assert "blacksmith" in card.content
        assert "he/him" in card.content

    def test_id_is_case_folded_name_namespaced(self) -> None:
        """Epic-72 identity is a case-folded name string; the card id must be
        the stable namespaced projection of it."""
        member = NpcPoolMember(name="Borin", drawn_from="world_authored")
        card = project_npc_card(member)
        assert card.id == "npc:borin"

    def test_disposition_tier_projected(self) -> None:
        """AC-2: disposition *tier* (attitude band), not the raw int, belongs in
        the embeddable content so retrieval keys on relationship."""
        member = NpcPoolMember(
            name="Skarl",
            role="enforcer",
            disposition=Disposition(-40),
            drawn_from="world_authored",
        )
        card = project_npc_card(member)
        assert Attitude.HOSTILE.value in card.content

    def test_projection_is_deterministic(self) -> None:
        """ADR-118 D3 dual-rep risk: same state → same content every time, so
        embeddings do not churn on reproject (75-6 relies on this)."""
        member = NpcPoolMember(
            name="Borin", role="smith", pronouns="he/him", drawn_from="world_authored"
        )
        assert project_npc_card(member).content == project_npc_card(member).content

    def test_pending_for_embedding_on_projection(self) -> None:
        member = NpcPoolMember(name="Borin", drawn_from="world_authored")
        card = project_npc_card(member)
        assert card.embedding_pending is True
        assert card.embedding is None


# ---------------------------------------------------------------------------
# AC-2 — Faction projector (reads the real Faction struct)
# ---------------------------------------------------------------------------


class TestFactionProjector:
    def test_projects_goals_and_attitude(self) -> None:
        """AC-2: faction card carries goals/summary, attitude, name."""
        faction = Faction(
            name="Tide Syndicate",
            summary="Controls the wet docks and the smuggling lanes.",
            description="A cartel of dockworkers turned smugglers.",
            disposition="hostile",
        )
        card = project_faction_card(faction)
        assert card.entity_type == "faction"
        assert "Tide Syndicate" in card.content
        assert "smuggling" in card.content
        assert "hostile" in card.content

    def test_id_namespaced_and_stable(self) -> None:
        faction = Faction(
            name="Tide Syndicate", summary="s", description="d", disposition=""
        )
        card = project_faction_card(faction)
        assert card.id == "faction:tide_syndicate"

    def test_deterministic(self) -> None:
        faction = Faction(
            name="Tide Syndicate", summary="s", description="d", disposition=""
        )
        assert project_faction_card(faction).content == project_faction_card(faction).content


# ---------------------------------------------------------------------------
# AC-2 — Location projector (normalized view; diffuse-source adaptation
# deferred to the consumer per ADR-118 permitted v1 — see TEA deviation)
# ---------------------------------------------------------------------------


class TestLocationProjector:
    def test_projects_name_description_and_mechanics(self) -> None:
        card = project_location_card(
            location_id="black_hart",
            name="The Black Hart",
            description="A low-beamed dockside tavern thick with pipe smoke.",
            mechanical_properties={"cover": "heavy", "lighting": "dim"},
        )
        assert card.entity_type == "location"
        assert card.id == "loc:black_hart"
        assert "Black Hart" in card.content
        assert "dockside tavern" in card.content

    def test_linked_npcs_included(self) -> None:
        card = project_location_card(
            location_id="black_hart",
            name="The Black Hart",
            description="A tavern.",
            linked_npcs=["Borin", "Skarl"],
        )
        assert "Borin" in card.content
        assert "Skarl" in card.content

    def test_blank_description_rejected(self) -> None:
        """No Silent Fallbacks: a location with no projectable text must fail
        loud, not emit an empty card."""
        with pytest.raises(ValueError):
            project_location_card(
                location_id="void", name="", description="   "
            )
