"""Story 84-3 (WI-4) — EntityType.RELATIONSHIP registration + 84-1 fail-loud guard (RED).

ADR-118 §A2 adds ``relationship`` as an indexed entity type. The 84-1 scorer
(``pertinence.py``) FAILS LOUD on any ``EntityType`` not in ``SIGNAL_APPLICABILITY``
(``_applicable_signals`` raises ``ValueError`` — No Silent Fallbacks). So adding the
enum member WITHOUT the three registrations breaks the live scorer the moment a
relationship card is scored in ``retrieve_turn_context``. This suite pins that the
enum + ``_ID_NAMESPACE`` + ``SIGNAL_APPLICABILITY`` land TOGETHER, with no fail-loud
regression.

THE THREE REGISTRATIONS (the test IS the spec):
  * ``EntityType.RELATIONSHIP == "relationship"``
  * ``_ID_NAMESPACE[RELATIONSHIP] == "rel"``  (else EntityCard.new raises)
  * ``SIGNAL_APPLICABILITY[RELATIONSHIP]`` declared  (else score_card raises)
    — recommended ``{mention, here, recency}`` (a relationship rides the related
    NPC's structural signals; topical cosine is the NPC card's job, §A2).

Synthetic fixtures only. Run ``-n0`` if any test triggers a scorer span.
"""

from __future__ import annotations

import pytest

# ===========================================================================
# AC-4 — the three registrations
# ===========================================================================


class TestRelationshipEntityTypeRegistration:
    def test_relationship_entity_type_registered(self) -> None:
        """``EntityType`` must expose ``RELATIONSHIP`` (§A2 index-side type)."""
        from sidequest.game.entity_card import EntityType

        assert hasattr(EntityType, "RELATIONSHIP"), "EntityType.RELATIONSHIP must exist"
        assert EntityType.RELATIONSHIP == "relationship"

    def test_relationship_in_id_namespace(self) -> None:
        """``EntityCard.new(RELATIONSHIP, ...)`` must not raise — the id namespace
        must be registered (else _ID_NAMESPACE lookup raises ValueError)."""
        from sidequest.game.entity_card import EntityCard, EntityType

        card = EntityCard.new(EntityType.RELATIONSHIP, "borin", content="Borin — friendly")
        assert card.id == "rel:borin", f"relationship id must be rel:<slug>, got {card.id!r}"

    def test_relationship_in_signal_applicability(self) -> None:
        """The 84-1 ``SIGNAL_APPLICABILITY`` matrix MUST declare RELATIONSHIP —
        an undeclared type makes ``_applicable_signals`` raise."""
        from sidequest.game.entity_card import EntityType
        from sidequest.game.pertinence import SIGNAL_APPLICABILITY

        declared = SIGNAL_APPLICABILITY.get(EntityType.RELATIONSHIP) or SIGNAL_APPLICABILITY.get(
            str(EntityType.RELATIONSHIP)
        )
        assert declared is not None, (
            "SIGNAL_APPLICABILITY must declare RELATIONSHIP or the 84-1 scorer fails loud"
        )
        assert len(declared) > 0, "a relationship card must have at least one applicable signal"

    def test_relationship_applicable_signals_include_mention(self) -> None:
        """§A2: a relationship surfaces because the related NPC is named/present —
        ``mention`` must be applicable so an alias/name reference pulls it in."""
        from sidequest.game.entity_card import EntityType
        from sidequest.game.pertinence import SIGNAL_APPLICABILITY, SIGNAL_MENTION

        declared = SIGNAL_APPLICABILITY.get(EntityType.RELATIONSHIP) or SIGNAL_APPLICABILITY.get(
            str(EntityType.RELATIONSHIP)
        )
        assert declared is not None
        assert SIGNAL_MENTION in declared, "mention must apply to a relationship card (§A2)"


# ===========================================================================
# AC-4 — NO fail-loud regression in the 84-1 scorer
# ===========================================================================


class TestNoFailLoudRegression:
    def test_score_card_on_relationship_does_not_fail_loud(self) -> None:
        """Scoring a relationship card through the live 84-1 ``score_card`` must
        NOT raise the ``_applicable_signals`` ValueError — proving the type is
        fully registered, not half-added."""
        from sidequest.game.entity_card import EntityCard, EntityType
        from sidequest.game.pertinence import PertinenceSignals, score_card

        card = EntityCard.new(
            EntityType.RELATIONSHIP, "borin", content="Borin — friendly — saved the party"
        )
        signals = PertinenceSignals(
            mention=1.0, here=0.0, recency=0.5, sim=None, present_scene=False
        )
        # Must not raise; must produce a finite score.
        result = score_card(card, signals)
        assert result.card_id == "rel:borin"
        assert isinstance(result.score, float)

    def test_unregistered_type_still_fails_loud(self) -> None:
        """Guard the guard: the fail-loud mechanism itself is intact — a genuinely
        bogus entity_type still raises (we didn't neuter No Silent Fallbacks)."""
        from sidequest.game.entity_card import EntityCard, EntityType
        from sidequest.game.pertinence import PertinenceSignals, score_card

        # Construct a real card, then force a bogus entity_type to prove the
        # scorer still rejects an undeclared type.
        card = EntityCard.new(EntityType.NPC, "borin", content="Borin")
        bogus = card.model_copy(update={"entity_type": "totally_unknown_type"})
        signals = PertinenceSignals(mention=0.0, here=0.0, recency=0.0, sim=None, present_scene=False)
        with pytest.raises(ValueError):
            score_card(bogus, signals)
