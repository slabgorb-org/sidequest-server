"""Relationship payload builder (ADR-136).

Converts engine NPC state (``snapshot.npcs``) into protocol ``RelationshipEntry``
payloads. Presentation logic (the 5-level display band, trend derivation,
personality read) lives here, keeping the engine ``Attitude`` enum and
``OceanProfile`` untouched. The claims-only firewall (Phase C) is applied here at
build time — disposition/OCEAN/claims are global, so there is no per-recipient
projection fork.
"""

from __future__ import annotations

from typing import Any

from sidequest.game.disposition import DispositionBeat
from sidequest.protocol.models import (
    DispositionBeatPayload,
    RelationshipEntry,
)

# 5-level DISPLAY band (ADR-136). Distinct from the engine 3-level Attitude enum.
_TREND_WINDOW = 3


def band_for(value: int) -> str:
    """Map a raw disposition value (-100..+100) to the 5-level display band."""
    if value >= 50:
        return "Devoted"
    if value >= 10:
        return "Warm"
    if value > -10:
        return "Neutral"
    if value > -50:
        return "Cool"
    return "Hostile"


def trend_for(beats: list[DispositionBeat], k: int = _TREND_WINDOW) -> str:
    """Derive trend from the sign of the summed deltas in the recent window."""
    recent = beats[-k:]
    total = sum(b.delta for b in recent)
    if total > 0:
        return "up"
    if total < 0:
        return "down"
    return "flat"


def build_relationship_entries(snapshot: Any) -> list[RelationshipEntry]:
    """Build one RelationshipEntry per NPC in ``snapshot.npcs``.

    Phase A: band/disposition/trend/last-seen/beats. OCEAN (Phase B) and claims
    (Phase C) ship empty here and are populated by later phases.
    """
    entries: list[RelationshipEntry] = []
    for npc in snapshot.npcs:
        value = int(npc.disposition)
        beats = [
            DispositionBeatPayload(turn=b.turn, delta=b.delta, reason=b.reason, location=b.location)
            for b in npc.disposition_log
        ]
        entries.append(
            RelationshipEntry(
                name=npc.core.name,
                portrait_url=None,  # Phase A: no portrait wiring (absence shown as absence)
                band=band_for(value),
                disposition=value,
                trend=trend_for(npc.disposition_log),
                last_seen_turn=npc.last_seen_turn,
                last_seen_location=npc.last_seen_location,
                beats=beats,
                personality_read=personality_read(npc.ocean),
                ocean=npc.ocean,
                claims=[],
            )
        )
    return entries


# Narrative descriptors per OCEAN dimension at the high / low pole (ADR-040
# narrative read; the numeric profile is the reveal detail).
_OCEAN_DESCRIPTORS: dict[str, tuple[str, str]] = {
    "openness": ("curious and imaginative", "conventional and grounded"),
    "conscientiousness": ("disciplined and reliable", "careless and impulsive"),
    "extraversion": ("outgoing and warm", "reserved and private"),
    "agreeableness": ("gracious and trusting", "abrasive and blunt"),
    "neuroticism": ("anxious and volatile", "calm and steady"),
}


def personality_read(ocean: dict[str, float] | None) -> str | None:
    """Narrative personality read from an OceanProfile dump (full keys, 0..10).

    Picks the up-to-two most salient dimensions (furthest from the 5.0 center,
    distance >= 2.0). A flat profile reads as even-keeled. None in, None out —
    absence is shown as absence, never a fabricated personality.
    """
    if not ocean:
        return None
    scored = sorted(
        ((dim, val - 5.0) for dim, val in ocean.items() if dim in _OCEAN_DESCRIPTORS),
        key=lambda kv: abs(kv[1]),
        reverse=True,
    )
    salient = [(dim, dist) for dim, dist in scored if abs(dist) >= 2.0][:2]
    if not salient:
        return "Even-keeled and hard to read."
    phrases = [
        _OCEAN_DESCRIPTORS[dim][0] if dist > 0 else _OCEAN_DESCRIPTORS[dim][1]
        for dim, dist in salient
    ]
    return f"{'; '.join(phrases).capitalize()}."


def _credibility_hint(claim: Any, belief_state: Any) -> str:
    """Coarse credibility bucket for a claim.

    Prefer the credibility score of the claim's source NPC (when told_by);
    fall back to the claim's own ``believed`` flag.
    """
    source = claim.source
    score: float | None = None
    if getattr(source, "kind", "") == "told_by":
        cred = belief_state.credibility_scores.get(source.by)
        if cred is not None:
            score = cred.score
    if score is None:
        return "credible" if claim.believed else "doubtful"
    if score >= 0.66:
        return "credible"
    if score >= 0.33:
        return "uncertain"
    return "doubtful"


def claims_to_party(belief_state: Any) -> list[dict]:
    """Filter belief_state to Claim entries only — the spoiler firewall (ADR-136).

    Fact and Suspicion entries are the mystery's solution (ADR-053) and are
    dropped entirely. Only Claim entries (statements in the NPC's knowledge,
    sourced from others) cross, each with a coarse credibility hint.
    """
    out: list[dict] = []
    for belief in belief_state.beliefs:
        if getattr(belief, "variant", "") != "claim":
            continue
        out.append(
            {"text": belief.content, "credibility_hint": _credibility_hint(belief, belief_state)}
        )
    return out
