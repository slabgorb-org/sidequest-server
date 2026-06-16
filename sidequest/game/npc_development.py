"""Interest-driven NPC development tick (Story 72-1, ADR-014 / ADR-020).

Revives the dormant development pipeline. On each *non-transactional*
engagement — a narrator cite that resolves to an existing stateful ``Npc``
(the ``npcs_hit`` branch of ``narration_apply._apply_npc_mentions``) — the
NPC earns depth:

1. ``non_transactional_interactions`` increments (the interest signal),
2. ``resolution_tier`` escalates up a named ladder at fixed thresholds,
3. ``disposition`` warms slightly through the clamping ``Disposition``
   constructor.

This mirrors ADR-014's coal->diamond promotion *on genuine player interest*
rather than the prior mechanical-necessity-only path (combat / status / MM
materialization), and feeds ADR-020 disposition evolution.

Thresholds and drift live here as named constants — never magic literals
scattered through the apply branch. The caller emits the development-tick and
``disposition.shift`` OTEL spans from the ``DevelopmentTick`` this returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sidequest.game.disposition import Disposition

if TYPE_CHECKING:
    from sidequest.game.session import Npc

# Interest-count thresholds at which the resolution tier escalates up the
# monotonic ladder spawn -> acquaintance -> established (ADR-020 enrichment).
# ``spawn`` is the materialization default; the higher tiers are earned through
# sustained non-transactional engagement. The first threshold is > 1 so a
# single engagement (or a lone combat hit) never promotes — depth is earned
# over several turns, per the "talks for ten turns" framing.
ACQUAINTANCE_AT = 3
ESTABLISHED_AT = 8

# Per-MILESTONE disposition warm (ping-pong 2026-06-07 "naive +2-attention
# model"). The original design warmed +2 on EVERY cite — engagement-count
# masquerading as valence (operator diagnosis: firing on the Thari cutter
# every round read as "Warm ↗"; Mrs. Poole warmed by being NAMED in someone
# else's dialogue). Attention is INTEREST (ADR-014 coal→diamond), not VALENCE
# (ADR-020/136): the tier ladder stays attention-driven, but disposition now
# moves only when a rapport MILESTONE is crossed (tier escalation) — sustained
# engagement still "deepens rapport", just at the earned thresholds, not per
# mention. Valenced movement is the narrator's ``update_npc_disposition`` tool
# (signed delta + reason → disposition beat), which this drift must never
# drown out. Applied via the clamping ``Disposition`` constructor.
DISPOSITION_DRIFT_PER_MILESTONE = 2


def engagement_beat_reason(tier: str) -> str:
    """ADR-136 beat reason for a rapport-milestone drift, naming the tier."""
    return f"rapport deepened — now {tier}"


def tier_for_interactions(interactions: int) -> str:
    """Map an interest count to its resolution tier (monotonic, named ladder)."""
    if interactions >= ESTABLISHED_AT:
        return "established"
    if interactions >= ACQUAINTANCE_AT:
        return "acquaintance"
    return "spawn"


@dataclass(frozen=True)
class DevelopmentTick:
    """Before/after deltas of one development tick, for OTEL emission."""

    interactions: int
    tier_before: str
    tier_after: str
    disposition_before: int
    disposition_after: int
    attitude_before: str
    attitude_after: str

    @property
    def disposition_delta(self) -> int:
        return self.disposition_after - self.disposition_before

    @property
    def attitude_crossed(self) -> bool:
        return self.attitude_before != self.attitude_after


def develop_npc_on_engagement(npc: Npc) -> DevelopmentTick:
    """Apply one interest-driven development tick to ``npc`` in place.

    Increments the interest counter and escalates ``resolution_tier`` per the
    named ladder. Disposition warms ONLY when this tick crosses a rapport
    milestone (tier escalation) — never per bare cite (ping-pong 2026-06-07:
    attention is interest, not valence). Returns the before/after deltas so
    the caller can emit the development-tick and (when disposition actually
    moved) the ``disposition.shift`` span + the ADR-136 milestone beat.
    """
    tier_before = npc.resolution_tier
    disposition_before = int(npc.disposition)
    attitude_before = npc.disposition.attitude().value

    npc.non_transactional_interactions += 1
    npc.resolution_tier = tier_for_interactions(npc.non_transactional_interactions)
    if npc.resolution_tier != tier_before:
        npc.disposition = Disposition(disposition_before + DISPOSITION_DRIFT_PER_MILESTONE)

    return DevelopmentTick(
        interactions=npc.non_transactional_interactions,
        tier_before=tier_before,
        tier_after=npc.resolution_tier,
        disposition_before=disposition_before,
        disposition_after=int(npc.disposition),
        attitude_before=attitude_before,
        attitude_after=npc.disposition.attitude().value,
    )
