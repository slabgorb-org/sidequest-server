"""Spoiler-firewall tests for ``claims_to_party`` (ADR-136, Phase C).

The firewall is the single most security-sensitive function in the relationship
panel: an NPC's ``belief_state`` mixes ``Fact`` and ``Suspicion`` entries (the
mystery's solution per ADR-053) with ``Claim`` entries (statements the NPC
relays from others). Only ``Claim`` may ever cross to players. These tests plant
``Fact`` + ``Suspicion`` entries carrying "SECRET" alongside a benign ``Claim``
and assert the secrets never leak.
"""

from sidequest.game.belief_state import (
    BeliefClaim,
    BeliefFact,
    BeliefSourceToldBy,
    BeliefState,
    BeliefSuspicion,
    Credibility,
)
from sidequest.game.projection.relationships import claims_to_party


def test_only_claims_cross_the_firewall():
    bs = BeliefState(
        beliefs=[
            BeliefFact(
                subject="murder",
                content="SECRET: the butler did it",
                source=BeliefSourceToldBy(by="self"),
            ),
            BeliefSuspicion.make(
                subject="murder",
                content="SECRET: I suspect the maid",
                turn_learned=1,
                source=BeliefSourceToldBy(by="self"),
                confidence=0.6,
            ),
            BeliefClaim(
                subject="alibi",
                content="I was in the garden all evening",
                source=BeliefSourceToldBy(by="Tabitha"),
                believed=True,
            ),
        ],
        credibility_scores={"Tabitha": Credibility.new(0.8)},
    )
    claims = claims_to_party(bs)
    texts = [c["text"] for c in claims]
    assert "I was in the garden all evening" in texts
    # the firewall is load-bearing: NO secret crosses
    assert not any("SECRET" in t for t in texts)
    assert len(claims) == 1


def test_credibility_hint_buckets():
    bs = BeliefState(
        beliefs=[
            BeliefClaim(
                subject="x",
                content="trusted claim",
                source=BeliefSourceToldBy(by="Trusted"),
                believed=True,
            ),
            BeliefClaim(
                subject="y",
                content="dubious claim",
                source=BeliefSourceToldBy(by="Shady"),
                believed=False,
            ),
        ],
        credibility_scores={
            "Trusted": Credibility.new(0.9),
            "Shady": Credibility.new(0.1),
        },
    )
    by_text = {c["text"]: c["credibility_hint"] for c in claims_to_party(bs)}
    assert by_text["trusted claim"] == "credible"
    assert by_text["dubious claim"] == "doubtful"


def test_uncertain_bucket_and_believed_fallback():
    """Mid-range source cred -> uncertain; missing source cred -> believed flag."""
    bs = BeliefState(
        beliefs=[
            BeliefClaim(
                subject="a",
                content="mid claim",
                source=BeliefSourceToldBy(by="Middling"),
                believed=False,
            ),
            BeliefClaim(
                subject="b",
                content="unknown-source believed",
                source=BeliefSourceToldBy(by="Stranger"),
                believed=True,
            ),
        ],
        credibility_scores={"Middling": Credibility.new(0.5)},
    )
    by_text = {c["text"]: c["credibility_hint"] for c in claims_to_party(bs)}
    assert by_text["mid claim"] == "uncertain"
    # No credibility score for "Stranger" -> fall back to the believed flag.
    assert by_text["unknown-source believed"] == "credible"
