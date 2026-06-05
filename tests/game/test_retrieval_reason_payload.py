"""Story 84-4 (WI-6) — per-card reason payload pure helpers (RED phase).

ADR-118 Amendment §A5: each *selected* card emits its score breakdown so the GM
panel shows WHY a note surfaced. 84-1 (WI-1) already populates
``RetrievedEntities.card_scores: list[PertinenceScore]`` — each ``PertinenceScore``
carries the four per-signal CONTRIBUTIONS (mention/here/recency/sim), the final
``score``, ``present_scene``, and ``embed_used``. WI-6 turns that struct into a
GM-panel-readable reason payload.

THE CONTRACT THIS SUITE PINS (the test IS the spec) — net-new pure helpers:

    # sidequest.telemetry.retrieval_reason  (NET-NEW module — pure, no I/O)

    def dominant_signal(score: PertinenceScore) -> str
        # the name of the highest-CONTRIBUTING signal: "mention" | "here" |
        # "recency" | "sim". Ties resolve by the §A1 priority order
        # mention > here > recency > sim (mention dominates).

    def card_reason_payload(score: PertinenceScore) -> dict
        # JSON-serializable; keys:
        #   card_id:   str
        #   mention:   float   (the mention CONTRIBUTION)
        #   here:      float
        #   recency:   float
        #   sim:       float
        #   score:     float
        #   dominant:  str     (== dominant_signal(score))
        #   embed_used: bool
        #   tier:      str     (the card's PRESENT projection tier — one tier
        #                       today; WI-3/84-6 adds demotion. Honest, not faked.)

These are PURE: no daemon, no span, no store. Synthetic ``PertinenceScore``
fixtures only (project rule: unit tests test code with synthetic fixtures).
Symbols imported inside each test so collection survives and each fails with a
crisp ImportError/AttributeError until Dev (Naomi) implements them.
"""

from __future__ import annotations

import json

import pytest

# Signal-name string constants reused from the scorer so the reason payload keys
# agree with the §A1 vocabulary (no drift between scorer and telemetry).
from sidequest.game.pertinence import (
    SIGNAL_HERE,
    SIGNAL_MENTION,
    SIGNAL_RECENCY,
    SIGNAL_SIM,
    PertinenceScore,
)


def _score(
    *,
    card_id: str = "npc:borin",
    mention: float = 0.0,
    here: float = 0.0,
    recency: float = 0.0,
    sim: float = 0.0,
    present_scene: bool = False,
    embed_used: bool = True,
) -> PertinenceScore:
    """A synthetic PertinenceScore. The four floats are CONTRIBUTIONS
    (weight·signal), matching what 84-1's ``score_card`` produces."""
    return PertinenceScore(
        card_id=card_id,
        mention_contribution=mention,
        here_contribution=here,
        recency_contribution=recency,
        sim_contribution=sim,
        score=mention + here + recency + sim,
        present_scene=present_scene,
        embed_used=embed_used,
    )


# ===========================================================================
# AC-1 — dominant signal
# ===========================================================================


class TestDominantSignal:
    def test_dominant_signal_picks_largest_contribution(self) -> None:
        """The headline 'why it surfaced' is the signal whose CONTRIBUTION (not
        raw value) is largest. Here ``here`` dominates an otherwise-named card."""
        from sidequest.telemetry.retrieval_reason import dominant_signal

        score = _score(mention=0.1, here=0.9, recency=0.0, sim=0.2)
        assert dominant_signal(score) == SIGNAL_HERE

    def test_dominant_signal_mention_when_mention_leads(self) -> None:
        from sidequest.telemetry.retrieval_reason import dominant_signal

        score = _score(mention=1.0, here=0.4, recency=0.2, sim=0.3)
        assert dominant_signal(score) == SIGNAL_MENTION

    def test_dominant_signal_sim_when_only_similarity(self) -> None:
        """A purely topical fill (the cosine fallback won) reports ``sim``."""
        from sidequest.telemetry.retrieval_reason import dominant_signal

        score = _score(mention=0.0, here=0.0, recency=0.0, sim=0.3)
        assert dominant_signal(score) == SIGNAL_SIM

    def test_dominant_signal_breaks_ties_by_priority_order(self) -> None:
        """§A1 priority mention > here > recency > sim. When two contributions
        tie EXACTLY, the higher-priority signal wins — deterministic, never
        arbitrary dict ordering. mention==here==0.5 → mention."""
        from sidequest.telemetry.retrieval_reason import dominant_signal

        tie_mention_here = _score(mention=0.5, here=0.5, recency=0.0, sim=0.0)
        assert dominant_signal(tie_mention_here) == SIGNAL_MENTION

        tie_here_recency = _score(mention=0.0, here=0.5, recency=0.5, sim=0.0)
        assert dominant_signal(tie_here_recency) == SIGNAL_HERE

        tie_recency_sim = _score(mention=0.0, here=0.0, recency=0.5, sim=0.5)
        assert dominant_signal(tie_recency_sim) == SIGNAL_RECENCY


# ===========================================================================
# AC-1 — card_reason_payload
# ===========================================================================


class TestCardReasonPayload:
    def test_card_reason_payload_has_all_signal_contributions(self) -> None:
        """The payload carries the card id, all four signal contributions, the
        final score, the dominant signal, embed_used, and tier."""
        from sidequest.telemetry.retrieval_reason import card_reason_payload

        score = _score(
            card_id="loc:black_hart",
            mention=0.0,
            here=0.4,
            recency=0.1,
            sim=0.3,
            embed_used=True,
        )
        payload = card_reason_payload(score)

        assert payload["card_id"] == "loc:black_hart"
        assert payload[SIGNAL_MENTION] == pytest.approx(0.0)
        assert payload[SIGNAL_HERE] == pytest.approx(0.4)
        assert payload[SIGNAL_RECENCY] == pytest.approx(0.1)
        assert payload[SIGNAL_SIM] == pytest.approx(0.3)
        assert payload["score"] == pytest.approx(0.8)
        assert payload["dominant"] == SIGNAL_HERE
        assert payload["embed_used"] is True
        assert "tier" in payload, "the payload must report the card's present tier"

    def test_card_reason_payload_dominant_matches_dominant_signal(self) -> None:
        """The payload's ``dominant`` must equal ``dominant_signal`` — one source
        of truth, no divergent re-derivation."""
        from sidequest.telemetry.retrieval_reason import (
            card_reason_payload,
            dominant_signal,
        )

        score = _score(mention=0.9, here=0.1, recency=0.0, sim=0.2)
        assert card_reason_payload(score)["dominant"] == dominant_signal(score)

    def test_card_reason_payload_is_json_serializable(self) -> None:
        """The span attribute is a JSON-encoded list and the watcher field is a
        list of these dicts — the payload MUST round-trip through json.dumps
        (no dataclasses, no enums, no floats-as-objects)."""
        from sidequest.telemetry.retrieval_reason import card_reason_payload

        payload = card_reason_payload(_score(mention=1.0, sim=0.2))
        encoded = json.dumps(payload)  # must not raise
        decoded = json.loads(encoded)
        assert decoded["card_id"] == "npc:borin"
        assert decoded["dominant"] == SIGNAL_MENTION

    def test_card_reason_payload_reports_present_tier(self) -> None:
        """AC-5: the present projection tier is reported honestly. There is one
        tier today (WI-3/84-6 adds demotion) — the value must be a non-empty
        string, not a fabricated demoted/INDEX tier."""
        from sidequest.telemetry.retrieval_reason import card_reason_payload

        tier = card_reason_payload(_score(mention=1.0))["tier"]
        assert isinstance(tier, str) and tier, "tier must be a non-empty string"

    def test_card_reason_payload_skipped_embed_reports_embed_used_false(self) -> None:
        """When the drama-gate skipped the cosine pass, ``embed_used`` is False in
        the payload — the GM panel must see the embed was bypassed (No Silent
        Fallbacks: never imply a similarity that wasn't computed)."""
        from sidequest.telemetry.retrieval_reason import card_reason_payload

        score = _score(mention=1.0, here=1.0, sim=0.0, embed_used=False)
        assert card_reason_payload(score)["embed_used"] is False
