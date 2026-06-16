"""Story 84-3 (WI-4) — relationship card RENDERS into the narrator prompt (RED).

THE REVIEWER BLOCKER, half 2: indexing + retrieval are nothing without a consumer.
``session_helpers._build_turn_context`` renders ``retrieved_npcs/locations/factions``
into typed ``TurnContext`` fields that ``Orchestrator.build_narrator_prompt`` injects
as Valley sections — but it renders NOTHING for ``retrieved_relationships``. So even
once retrieval surfaces a relationship card (the §A2 floor-companion path), the card
DIES at the render seam and never reaches the narrator. That is dead wiring.

This suite drives the REAL render chain (the 75-8 e2e harness):

    RetrievedEntities(retrieved_relationships=[rel card])
      → session_helpers._build_turn_context   (must render → context.retrieved_entity_relationships)
        → Orchestrator.build_narrator_prompt   (must register a retrieved_relationships Valley section)

Both seams currently no-op for relationships → RED. Behavior + assembled-prompt
sections only (No Source-Text Wiring Tests).
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.agents.npc_context import build_npc_working_set
from sidequest.agents.prompt_framework.types import AttentionZone
from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from tests._helpers.doubles import make_orchestrator


def _empty_floor() -> Any:
    snap = GameSnapshot(genre_slug="caverns_and_claudes", turn_manager=TurnManager(interaction=1))
    return build_npc_working_set(snap, current_turn=1)


def _retrieved_with_relationship(card: EntityCard) -> Any:
    """A ``RetrievedEntities`` carrying exactly one relationship card (the §A2
    floor-companion output), everything else empty."""
    from sidequest.game.retrieval_orchestration import RetrievedEntities

    return RetrievedEntities(
        floor=_empty_floor(),
        retrieved_npcs=None,
        retrieved_locations=None,
        retrieved_factions=None,
        budget_total=4000,
        floor_count=0,
        floor_token_cost=0,
        fill_candidate_count=0,
        fill_selected_count=0,
        fill_token_cost=0,
        rejected_below_similarity=0,
        dimension_mismatch_count=0,
        outcome="success",
        retrieved_relationships=[card],
    )


_REL_CONTENT = "Borin — Warm — saved the party from the ogre"


def _rel_card() -> EntityCard:
    return EntityCard.new(EntityType.RELATIONSHIP, "borin", content=_REL_CONTENT)


# ===========================================================================
# Seam 1 — session_helpers renders retrieved_relationships into TurnContext
# ===========================================================================


class TestSessionHelpersRendersRelationship:
    def test_build_turn_context_renders_relationship_section(
        self, session_handler_factory
    ) -> None:
        """``_build_turn_context`` must render a populated
        ``retrieved_relationships`` into a typed ``TurnContext`` field carrying the
        card's summary — mirroring ``retrieved_entity_npcs``. Today there is no such
        field/render, so the relationship summary never enters the context."""
        from sidequest.server.session_helpers import _build_turn_context

        sd, _handler = session_handler_factory(genre="caverns_and_claudes")
        result = _retrieved_with_relationship(_rel_card())

        context = _build_turn_context(sd, entity_retrieval=result)

        rendered = getattr(context, "retrieved_entity_relationships", None)
        assert rendered is not None, (
            "session_helpers must render retrieved_relationships into a "
            "retrieved_entity_relationships TurnContext field (the render seam is dead)"
        )
        assert "saved the party from the ogre" in rendered, (
            "the relationship card's summary must be in the rendered section"
        )


# ===========================================================================
# Seam 2 — the orchestrator injects it into the narrator prompt (Valley)
# ===========================================================================


class TestOrchestratorInjectsRelationship:
    @pytest.mark.asyncio
    async def test_relationship_section_reaches_narrator_prompt(
        self, session_handler_factory
    ) -> None:
        """The full §A2 deliverable: a relationship card must be INJECTED into the
        assembled narrator prompt as a ``retrieved_relationships`` Valley section,
        so the narrator actually sees the relationship standing + key beats for a
        present/named NPC. This is the end the Reviewer blocker is about."""
        from sidequest.server.session_helpers import _build_turn_context

        sd, _handler = session_handler_factory(genre="caverns_and_claudes")
        context = _build_turn_context(
            sd, entity_retrieval=_retrieved_with_relationship(_rel_card())
        )

        orch = make_orchestrator()
        _prompt, registry = await orch.build_narrator_prompt("I attack Borin", context)
        agent_name = orch._narrator.name()

        rel_sections = [
            s
            for s in registry.get_sections(agent_name, zone=AttentionZone.Valley)
            if s.name == "retrieved_relationships"
        ]
        assert len(rel_sections) == 1, (
            "the relationship card must register exactly one retrieved_relationships "
            "Valley section in the narrator prompt (the injection seam is dead)"
        )
        assert "saved the party from the ogre" in rel_sections[0].content, (
            "the relationship summary must reach the narrator prompt content"
        )

    @pytest.mark.asyncio
    async def test_no_relationship_registers_no_section(self, session_handler_factory) -> None:
        """Zero-byte-leak parity: with no relationship card, NO
        retrieved_relationships section is registered (an empty section would
        waste prompt budget)."""
        from sidequest.game.retrieval_orchestration import RetrievedEntities
        from sidequest.server.session_helpers import _build_turn_context

        sd, _handler = session_handler_factory(genre="caverns_and_claudes")
        empty = RetrievedEntities(
            floor=_empty_floor(),
            retrieved_npcs=None,
            retrieved_locations=None,
            retrieved_factions=None,
            budget_total=4000,
            floor_count=0,
            floor_token_cost=0,
            fill_candidate_count=0,
            fill_selected_count=0,
            fill_token_cost=0,
            rejected_below_similarity=0,
            dimension_mismatch_count=0,
            outcome="success",
            retrieved_relationships=None,
        )
        context = _build_turn_context(sd, entity_retrieval=empty)

        orch = make_orchestrator()
        _prompt, registry = await orch.build_narrator_prompt("I look around", context)
        agent_name = orch._narrator.name()
        rel_sections = [
            s
            for s in registry.get_sections(agent_name, zone=AttentionZone.Valley)
            if s.name == "retrieved_relationships"
        ]
        assert rel_sections == [], (
            "no relationship card → no retrieved_relationships section (zero-byte-leak)"
        )
