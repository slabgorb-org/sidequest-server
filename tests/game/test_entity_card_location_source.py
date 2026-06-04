"""RED-phase tests for Story 76-7 — location cards carry their *source origin*.

Story 76-7 AC2: "``project_location_card()`` … Location cards are tagged with
their source origin." Locations are diffuse across three sources (room graph,
``world_materialization``, and PG ``location_promotions`` — ADR-118 §D3 /
§Consequences). A retrieval card minted from a promotion must be distinguishable
from one minted off the authored room graph, both for GM-panel forensics (which
source fed the index?) and for the tiered-forgetting work that follows (ADR-118
amendment, epic 84). The projector already exists and emits a well-formed
``LOCATION`` card; what it does NOT yet do is record *where the location came
from* in the card metadata.

Contract sources (spec-authority order):
- ``sprint/context/context-story-76-7.md`` (AC2: source-origin tag)
- ADR-118 §D3 (per-source adaptation belongs to the consumer; the card is the
  uniform projection)

INTENTIONALLY RED until 76-7 lands: ``project_location_card`` takes no ``source``
argument today and always mints ``metadata={}`` (via ``EntityCard.new``'s default),
so there is no source origin to assert on.

Reuse discipline (SOUL *Don't Reinvent*): the source tag rides in the EXISTING
``EntityCard.metadata`` dict — no new field, no model change, the same metadata
channel the NPC projector already owns.
"""

from __future__ import annotations

import pytest

from sidequest.game.entity_card import EntityType, project_location_card

# The three diffuse location sources 76-7 must cover (ADR-118 §D3). The exact
# string keys are the implementer's to finalize, but every minted location card
# MUST record which of these fed it — that is the AC2 contract under test.
_ROOM_GRAPH = "room_graph"
_MATERIALIZATION = "world_materialization"
_PROMOTION = "promotion"


class TestLocationCardSourceOrigin:
    def test_projects_a_location_with_a_source_origin(self) -> None:
        """AC2: a location projects to a well-formed ``LOCATION`` card AND the
        card records the source it was minted from. Today ``project_location_card``
        accepts no ``source`` — this call raises ``TypeError`` (RED)."""
        card = project_location_card(
            location_id="black_hart",
            name="The Black Hart",
            description="A smoke-dark coaching inn off the old toll road.",
            source=_ROOM_GRAPH,
        )

        assert card.entity_type == EntityType.LOCATION
        assert card.id == "loc:black_hart"
        assert card.metadata.get("source") == _ROOM_GRAPH

    def test_source_origin_distinguishes_the_three_sources(self) -> None:
        """A location surfaced from a PG promotion must be distinguishable from
        the same-id location surfaced off the room graph — the metadata records
        provenance, so the GM panel can answer 'which source fed this card?'."""
        from_graph = project_location_card(
            location_id="black_hart",
            name="The Black Hart",
            description="A coaching inn.",
            source=_ROOM_GRAPH,
        )
        from_promotion = project_location_card(
            location_id="black_hart",
            name="The Black Hart",
            description="A coaching inn.",
            source=_PROMOTION,
        )

        assert from_graph.metadata["source"] == _ROOM_GRAPH
        assert from_promotion.metadata["source"] == _PROMOTION
        # Same location id-space (the basis for cross-source dedup at sync time),
        # different recorded provenance.
        assert from_graph.id == from_promotion.id
        assert from_graph.metadata["source"] != from_promotion.metadata["source"]

    def test_materialization_source_is_recorded(self) -> None:
        """The third source — ``world_materialization`` outputs — is tagged the
        same way; no source is silently dropped (No Silent Fallbacks)."""
        card = project_location_card(
            location_id="ember_terrace",
            name="The Ember Terrace",
            description="A materialized overlook of the caldera.",
            source=_MATERIALIZATION,
        )

        assert card.metadata["source"] == _MATERIALIZATION

    def test_source_origin_does_not_corrupt_the_card_content(self) -> None:
        """The source tag rides in metadata, NOT in the embeddable ``content`` —
        provenance must not pollute the vector the narrator retrieves on."""
        card = project_location_card(
            location_id="black_hart",
            name="The Black Hart",
            description="A coaching inn.",
            source=_ROOM_GRAPH,
        )

        assert _ROOM_GRAPH not in card.content
        assert "The Black Hart" in card.content
        assert "coaching inn" in card.content

    def test_blank_description_still_fails_loud_with_a_source(self) -> None:
        """AC2 must not weaken the existing No-Silent-Fallbacks guard: a blank
        description is rejected at the projector boundary even when a source is
        supplied — a source tag is not a license to mint a degenerate card."""
        with pytest.raises(ValueError):
            project_location_card(
                location_id="void",
                name="Nowhere",
                description="   ",
                source=_ROOM_GRAPH,
            )
