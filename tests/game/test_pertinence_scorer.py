"""Story 84-1 (WI-1) — Unified pertinence scorer (RED phase).

ADR-118 **Amendment §A1** supersedes §D4's two-mechanism floor/fill split with
ONE weighted, scored selection:

    score(card) = w_mention·mention(card, action, aliases)   # dominant
                + w_location·here(card, snapshot)            # is it here / adjacent
                + w_recency·decay(card.last_seen, now)       # recently touched
                + w_sim·cosine(embed(action), card)          # topical fallback
    → rank all candidates, take until the per-turn token budget is exhausted.

Three hard rulings ride on top of the weighted sum (all net-new behavior):

  1. **Present-scene HARD invariant** — a card for an entity the player is
     physically engaging this turn cannot be budgeted/evicted out, regardless of
     its computed score. This is a constraint on the selection, NOT an emergent
     property of weights.
  2. **Drama-gated embedding** — the expensive ``w_sim·cosine`` term (it requires
     a daemon ``embed(action)`` round-trip) is SKIPPED when the structured
     signals (mention + here) already come back strong. "I attack Borin" resolves
     on mention + location; the embed is never computed. Strictly cheaper than the
     §D4 always-embed fill.
  3. **Per-type signal applicability** — each entity type declares WHICH of the
     four signals apply to it (``here`` is load-bearing for an NPC, inapplicable
     to a free-floating lore-ish card). The *weight* of each applicable signal is
     a single GLOBAL tuning vector — one knob, no per-type weight nonsense terms.

ALL of the symbols imported below are NET-NEW and do not exist yet — these tests
fail RED until Agent Smith (Dev) implements them. Each test imports inside its
body so collection succeeds and each fails with a clear ImportError /
AttributeError rather than collapsing the whole module on import.

THE CONTRACT THIS SUITE PINS (the test IS the spec):

    # sidequest.game.pertinence  (NET-NEW module)

    @dataclass(frozen=True)
    class PertinenceWeights:
        w_mention: float
        w_location: float
        w_recency: float
        w_sim: float
    DEFAULT_PERTINENCE_WEIGHTS: PertinenceWeights   # enforces mention > location > recency

    SIGNAL_MENTION   = "mention"
    SIGNAL_HERE      = "here"
    SIGNAL_RECENCY   = "recency"
    SIGNAL_SIM       = "sim"
    SIGNAL_APPLICABILITY: dict[str, frozenset[str]]   # per EntityType → applicable signals

    @dataclass(frozen=True)
    class PertinenceSignals:
        mention: float        # 0..1  alias/name match strength
        here: float           # 0..1  scene-present / adjacent
        recency: float        # 0..1  decayed last_seen
        sim: float | None     # 0..1 cosine, or None when the embed was skipped
        present_scene: bool   # the player is physically engaging this entity

    @dataclass(frozen=True)
    class PertinenceScore:
        card_id: str
        mention_contribution: float
        here_contribution: float
        recency_contribution: float
        sim_contribution: float
        score: float                  # the weighted sum (applicable signals only)
        present_scene: bool
        embed_used: bool              # False when the drama-gate skipped cosine

    def score_card(card, signals, weights=DEFAULT_PERTINENCE_WEIGHTS) -> PertinenceScore
    def structured_signals_sufficient(signals) -> bool   # the drama-gate predicate

Test discipline: server CLAUDE.md "No Source-Text Wiring Tests" — the wiring
assertion in ``tests/game/test_retrieval_orchestration.py`` /
``tests/game/test_pertinence_wiring.py`` drives the real handler + span. This
file is the scorer's UNIT spec with synthetic fixtures only (no content
invariants — those belong in the pack validator per project rule).
"""

from __future__ import annotations

import pytest

from sidequest.game.entity_card import EntityCard, EntityType

# ---------------------------------------------------------------------------
# Synthetic fixtures — cards + signal vectors (no content, no real daemon)
# ---------------------------------------------------------------------------


def _card(entity_type: str, entity_id: str, content: str = "a test card body") -> EntityCard:
    return EntityCard.new(entity_type, entity_id, content=content)


def _signals(
    *,
    mention: float = 0.0,
    here: float = 0.0,
    recency: float = 0.0,
    sim: float | None = None,
    present_scene: bool = False,
):
    """Build a ``PertinenceSignals`` if the type exists; the import lives here so
    a missing module surfaces as the failure under test, not a collection error."""
    from sidequest.game.pertinence import PertinenceSignals

    return PertinenceSignals(
        mention=mention,
        here=here,
        recency=recency,
        sim=sim,
        present_scene=present_scene,
    )


# ===========================================================================
# Contract / module surface
# ===========================================================================


class TestPertinenceModuleSurface:
    def test_module_exposes_scorer_symbols(self) -> None:
        """The net-new module exports the weights, signal/score structs, the
        applicability matrix, the scorer, and the drama-gate predicate."""
        from sidequest.game.pertinence import (  # noqa: F401
            DEFAULT_PERTINENCE_WEIGHTS,
            SIGNAL_APPLICABILITY,
            PertinenceScore,
            PertinenceSignals,
            PertinenceWeights,
            score_card,
            structured_signals_sufficient,
        )

        assert callable(score_card)
        assert callable(structured_signals_sufficient)

    def test_default_weights_enforce_mention_dominates_location_dominates_recency(
        self,
    ) -> None:
        """§A1 resolved lean: ``mention ≫ location > recency``. The default global
        tuning vector must encode that strict ordering — not merely be positive."""
        from sidequest.game.pertinence import DEFAULT_PERTINENCE_WEIGHTS as w

        assert w.w_mention > w.w_location > w.w_recency > 0.0, (
            "mention must dominate location which must dominate recency "
            f"(got mention={w.w_mention} location={w.w_location} recency={w.w_recency})"
        )
        # Mention is DOMINANT, not just first — it should outweigh the other three
        # structured signals combined so a named entity always wins ranking.
        assert w.w_mention > (w.w_location + w.w_recency + w.w_sim), (
            "mention is the dominant signal (§A1) — it must outweigh "
            "location + recency + sim combined"
        )


# ===========================================================================
# §A1 — the weighted-sum formula
# ===========================================================================


class TestWeightedSum:
    def test_score_is_weighted_sum_of_applicable_signals(self) -> None:
        """The combined score is exactly Σ w_i·signal_i over the signals that
        apply to this entity type. NPCs use all four; here uses a flat fixture."""
        from sidequest.game.pertinence import (
            DEFAULT_PERTINENCE_WEIGHTS as w,
        )
        from sidequest.game.pertinence import (
            score_card,
        )

        card = _card(EntityType.NPC, "borin")
        sig = _signals(mention=1.0, here=1.0, recency=0.5, sim=0.25, present_scene=False)
        result = score_card(card, sig, w)

        expected = w.w_mention * 1.0 + w.w_location * 1.0 + w.w_recency * 0.5 + w.w_sim * 0.25
        assert result.score == pytest.approx(expected), (
            "score must be the weighted sum of the applicable signal values"
        )
        # The decomposition must be reported per-signal (feeds the A5 OTEL span).
        assert result.mention_contribution == pytest.approx(w.w_mention * 1.0)
        assert result.here_contribution == pytest.approx(w.w_location * 1.0)
        assert result.recency_contribution == pytest.approx(w.w_recency * 0.5)
        assert result.sim_contribution == pytest.approx(w.w_sim * 0.25)

    def test_named_entity_outranks_merely_similar_entity(self) -> None:
        """The whole point of §A1: a MENTIONED card beats a card that only has a
        high embedding similarity. Mention ≫ sim. This is the behavior the §D4
        floor/fill split could only approximate for NPCs."""
        from sidequest.game.pertinence import score_card

        named = _card(EntityType.NPC, "borin")
        similar = _card(EntityType.NPC, "stranger")

        named_score = score_card(named, _signals(mention=1.0, here=0.0, recency=0.0, sim=0.0))
        similar_score = score_card(similar, _signals(mention=0.0, here=0.0, recency=0.0, sim=1.0))

        assert named_score.score > similar_score.score, (
            "a named entity must outrank a merely-topically-similar one (mention ≫ sim)"
        )


# ===========================================================================
# §A1 — per-type signal applicability matrix
# ===========================================================================


class TestSignalApplicability:
    def test_applicability_declared_for_every_entity_type(self) -> None:
        """Each indexed entity type declares which of the four signals apply.
        Every ``EntityType`` member must appear in the matrix (No Silent
        Fallbacks: an un-declared type would silently score zero)."""
        from sidequest.game.pertinence import SIGNAL_APPLICABILITY

        for et in EntityType:
            assert et in SIGNAL_APPLICABILITY or str(et) in SIGNAL_APPLICABILITY, (
                f"entity type {et!r} must declare its applicable signals"
            )

    def test_npc_declares_here_as_applicable(self) -> None:
        """§A1 example: ``here`` is load-bearing for an NPC."""
        from sidequest.game.pertinence import SIGNAL_APPLICABILITY, SIGNAL_HERE

        npc_signals = SIGNAL_APPLICABILITY.get(EntityType.NPC) or SIGNAL_APPLICABILITY.get(
            str(EntityType.NPC)
        )
        assert npc_signals is not None
        assert SIGNAL_HERE in npc_signals, "an NPC's location (here) must be applicable"

    def test_inapplicable_signal_does_not_contribute_to_score(self) -> None:
        """If a type does not declare a signal, that signal's value is ignored —
        it contributes 0 even when the raw signal value is high. This is the
        'no nonsense terms' rule: a non-applicable signal cannot pollute ranking.

        We assert via a faction whose ``here`` is declared non-applicable (a
        faction is not physically in a room the way an NPC is). If the matrix
        DOES declare here for factions this test will need the Dev to confirm —
        the assertion pins the *mechanism*: a non-applicable signal is dropped."""
        from sidequest.game.pertinence import (
            SIGNAL_APPLICABILITY,
            SIGNAL_HERE,
            score_card,
        )

        fac_signals = SIGNAL_APPLICABILITY.get(EntityType.FACTION) or SIGNAL_APPLICABILITY.get(
            str(EntityType.FACTION)
        )
        assert fac_signals is not None
        if SIGNAL_HERE in fac_signals:
            pytest.skip("faction declares 'here' applicable; mechanism asserted elsewhere")

        card = _card(EntityType.FACTION, "tide_syndicate")
        with_here = score_card(card, _signals(here=1.0))
        without_here = score_card(card, _signals(here=0.0))
        assert with_here.score == pytest.approx(without_here.score), (
            "a non-applicable signal must contribute nothing to the score"
        )
        assert with_here.here_contribution == pytest.approx(0.0)


# ===========================================================================
# §A1 — present-scene HARD invariant
# ===========================================================================


class TestPresentSceneInvariant:
    def test_present_scene_flag_propagates_to_score(self) -> None:
        """The score records whether this entity is present-scene so the
        selection layer can enforce the hard invariant."""
        from sidequest.game.pertinence import score_card

        card = _card(EntityType.NPC, "borin")
        result = score_card(card, _signals(mention=0.0, present_scene=True))
        assert result.present_scene is True

    def test_present_scene_entity_survives_budget_even_with_low_score(self) -> None:
        """THE hard invariant (§A1): an entity the player is physically engaging
        cannot be budgeted out, even if its weighted score is the lowest of the
        pool. This is the §D4 floor guarantee re-expressed as a constraint on the
        ranked selection — not an emergent weight property.

        Pins the net-new selection helper ``select_within_budget``: present-scene
        cards are admitted FIRST and unconditionally; the remaining budget ranks
        the rest by score."""
        from sidequest.game.pertinence import score_card, select_within_budget

        # A low-scoring present-scene NPC and a high-scoring non-present card.
        present = _card(EntityType.NPC, "borin", content="Borin")  # cheap, present
        rich = _card(
            EntityType.LOCATION,
            "grand_hall",
            content="The Grand Hall is a sprawling vaulted chamber of immense size.",
        )
        scored = [
            score_card(present, _signals(mention=0.0, here=1.0, present_scene=True)),
            score_card(rich, _signals(mention=1.0, sim=1.0, present_scene=False)),
        ]
        cards_by_id = {present.id: present, rich.id: rich}

        # A budget large enough for ONLY the cheap present card.
        budget = present.token_estimate
        selected = select_within_budget(scored, cards_by_id, budget_tokens=budget)
        selected_ids = {c.id for c in selected}

        assert present.id in selected_ids, (
            "a present-scene entity must NEVER be budgeted out — hard invariant (§A1)"
        )

    def test_present_scene_not_dropped_even_when_budget_is_zero(self) -> None:
        """Boundary: even a zero remaining budget cannot evict the present scene.
        The invariant is absolute — the present scene is exempt from the token
        ceiling entirely (ADR-014 Living World: the current scene is never
        dropped)."""
        from sidequest.game.pertinence import score_card, select_within_budget

        present = _card(EntityType.NPC, "borin", content="Borin is right here.")
        scored = [score_card(present, _signals(present_scene=True))]
        cards_by_id = {present.id: present}

        selected = select_within_budget(scored, cards_by_id, budget_tokens=0)
        assert present.id in {c.id for c in selected}, (
            "zero budget must still admit the present scene — the invariant is absolute"
        )


# ===========================================================================
# §A1 — drama-gated embedding (SOUL: Cost Scales with Drama)
# ===========================================================================


class TestDramaGate:
    def test_strong_structured_signals_are_sufficient_skip_embed(self) -> None:
        """When mention + here come back strong, the structured signals suffice —
        the drama-gate returns True so the caller SKIPS the cosine embed.
        '"I attack Borin" resolves on mention + location.'"""
        from sidequest.game.pertinence import structured_signals_sufficient

        strong = _signals(mention=1.0, here=1.0, sim=None)
        assert structured_signals_sufficient(strong) is True, (
            "a named, located action has strong structured signals — skip the embed"
        )

    def test_thin_structured_signals_are_insufficient_require_embed(self) -> None:
        """When the action named nothing and the party is nowhere specific, the
        structured signals are thin — the gate returns False so the caller
        computes the cosine fallback. 'embedding similarity as the fallback'."""
        from sidequest.game.pertinence import structured_signals_sufficient

        thin = _signals(mention=0.0, here=0.0, sim=None)
        assert structured_signals_sufficient(thin) is False, (
            "an un-named, un-located action is thin — the embed must run as fallback"
        )

    def test_skipped_embed_marks_embed_used_false_and_sim_zero(self) -> None:
        """When the embed is skipped, the score must (a) report ``embed_used`` is
        False so the A5 ``retrieval.embed_skipped`` span is honest, and (b) treat
        the missing sim as a 0 contribution — never as a silent partial score that
        looks like a real similarity match (No Silent Fallbacks)."""
        from sidequest.game.pertinence import score_card

        card = _card(EntityType.NPC, "borin")
        # sim=None encodes "embed was skipped this turn".
        result = score_card(card, _signals(mention=1.0, here=1.0, sim=None))
        assert result.embed_used is False
        assert result.sim_contribution == pytest.approx(0.0), (
            "a skipped embed contributes 0 to sim — not a phantom similarity"
        )

    def test_present_embed_marks_embed_used_true(self) -> None:
        """The complement: when a real cosine value rode in, ``embed_used`` is
        True and the sim term contributes."""
        from sidequest.game.pertinence import DEFAULT_PERTINENCE_WEIGHTS as w
        from sidequest.game.pertinence import score_card

        card = _card(EntityType.NPC, "borin")
        result = score_card(card, _signals(mention=0.0, here=0.0, sim=0.8))
        assert result.embed_used is True
        assert result.sim_contribution == pytest.approx(w.w_sim * 0.8)
