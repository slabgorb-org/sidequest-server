"""Story 84-5 (WI-2) — QUEST + TROPE EntityType 3-registration + fail-loud guard (RED).

The KNOWN TRAP (hit on 84-3's RELATIONSHIP): the 84-1 scorer FAILS LOUD on any
EntityType not in SIGNAL_APPLICABILITY (pertinence._applicable_signals raises), and
EntityCard.new raises on a missing _ID_NAMESPACE entry. Adding QUEST/TROPE to the
enum alone breaks score_card the moment a quest/trope card is scored. This suite
pins that BOTH new types land all three registrations together with no fail-loud
regression.

Applicable signals (the §A2 decision): a quest/trope is NOT physically present like
an NPC/location, so ``here`` does NOT apply. A dormant note surfaces by MENTION or
topical SIMILARITY, decayed by RECENCY → ``{mention, recency, sim}``.

Synthetic fixtures only. Run ``-n0`` if any scorer span fires.
"""

from __future__ import annotations

import pytest

# ===========================================================================
# AC-4 — the three registrations, for both new types
# ===========================================================================


class TestQuestTropeRegistration:
    def test_quest_trope_entity_types_registered(self) -> None:
        from sidequest.game.entity_card import EntityType

        assert hasattr(EntityType, "QUEST"), "EntityType.QUEST must exist"
        assert hasattr(EntityType, "TROPE"), "EntityType.TROPE must exist"
        assert EntityType.QUEST == "quest"
        assert EntityType.TROPE == "trope"

    def test_quest_trope_in_id_namespace(self) -> None:
        """``EntityCard.new`` must not raise for either type — id namespace
        registered (else the _ID_NAMESPACE lookup raises)."""
        from sidequest.game.entity_card import EntityCard, EntityType

        q = EntityCard.new(EntityType.QUEST, "q1", content="A quest — completed")
        t = EntityCard.new(EntityType.TROPE, "t1", content="A trope — resolved")
        assert q.id == "quest:q1"
        assert t.id == "trope:t1"

    def test_quest_trope_in_signal_applicability(self) -> None:
        """The 84-1 matrix MUST declare both — an undeclared type makes
        ``_applicable_signals`` raise."""
        from sidequest.game.entity_card import EntityType
        from sidequest.game.pertinence import SIGNAL_APPLICABILITY

        for et in (EntityType.QUEST, EntityType.TROPE):
            declared = SIGNAL_APPLICABILITY.get(et) or SIGNAL_APPLICABILITY.get(str(et))
            assert declared is not None, (
                f"{et!r} must be in SIGNAL_APPLICABILITY (or scorer fails loud)"
            )
            assert len(declared) > 0

    def test_quest_trope_signals_exclude_here(self) -> None:
        """§A2: a quest/trope is not physically present — ``here`` must NOT apply;
        ``mention`` and ``sim`` must (it surfaces by name or topical similarity)."""
        from sidequest.game.entity_card import EntityType
        from sidequest.game.pertinence import (
            SIGNAL_APPLICABILITY,
            SIGNAL_HERE,
            SIGNAL_MENTION,
            SIGNAL_SIM,
        )

        for et in (EntityType.QUEST, EntityType.TROPE):
            declared = SIGNAL_APPLICABILITY.get(et) or SIGNAL_APPLICABILITY.get(str(et))
            assert declared is not None
            assert SIGNAL_HERE not in declared, f"{et!r} must NOT declare 'here' (not present)"
            assert SIGNAL_MENTION in declared, f"{et!r} must declare 'mention'"
            assert SIGNAL_SIM in declared, f"{et!r} must declare 'sim' (topical recall)"


# ===========================================================================
# AC-4 — no fail-loud regression in the 84-1 scorer
# ===========================================================================


class TestNoFailLoudRegression:
    def test_score_card_on_quest_does_not_fail_loud(self) -> None:
        from sidequest.game.entity_card import EntityCard, EntityType
        from sidequest.game.pertinence import PertinenceSignals, score_card

        card = EntityCard.new(EntityType.QUEST, "q1", content="The Smuggler's Debt — completed")
        signals = PertinenceSignals(
            mention=1.0, here=0.0, recency=0.4, sim=0.2, present_scene=False
        )
        result = score_card(card, signals)
        assert result.card_id == "quest:q1"
        assert isinstance(result.score, float)

    def test_score_card_on_trope_does_not_fail_loud(self) -> None:
        from sidequest.game.entity_card import EntityCard, EntityType
        from sidequest.game.pertinence import PertinenceSignals, score_card

        card = EntityCard.new(EntityType.TROPE, "t1", content="Redemption Arc — resolved")
        signals = PertinenceSignals(
            mention=0.0, here=0.0, recency=0.0, sim=0.7, present_scene=False
        )
        result = score_card(card, signals)
        assert result.card_id == "trope:t1"
        assert isinstance(result.score, float)

    def test_quest_here_signal_contributes_zero(self) -> None:
        """Since ``here`` is not applicable to a quest, a non-zero ``here`` signal
        contributes nothing to its score (per-type applicability drops it)."""
        from sidequest.game.entity_card import EntityCard, EntityType
        from sidequest.game.pertinence import PertinenceSignals, score_card

        card = EntityCard.new(EntityType.QUEST, "q1", content="A quest")
        with_here = score_card(
            card,
            PertinenceSignals(mention=0.0, here=1.0, recency=0.0, sim=0.0, present_scene=False),
        )
        assert with_here.here_contribution == 0.0, "here must not contribute to a quest score"

    def test_bogus_type_still_fails_loud(self) -> None:
        """Guard the guard: the No-Silent-Fallbacks fail-loud is intact — a bogus
        entity_type still raises (we didn't neuter it adding two new types)."""
        from sidequest.game.entity_card import EntityCard, EntityType
        from sidequest.game.pertinence import PertinenceSignals, score_card

        card = EntityCard.new(EntityType.QUEST, "q1", content="A quest")
        bogus = card.model_copy(update={"entity_type": "totally_unknown_type"})
        signals = PertinenceSignals(
            mention=0.0, here=0.0, recency=0.0, sim=0.0, present_scene=False
        )
        with pytest.raises(ValueError):
            score_card(bogus, signals)
