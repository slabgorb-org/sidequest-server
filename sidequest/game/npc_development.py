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

# Per-engagement disposition warm. Small and positive: sustained, non-hostile
# contact deepens rapport (ADR-020 "evolves through interaction"). Applied via
# the ``Disposition`` constructor, which clamps to +-100 — no unbounded growth.
DISPOSITION_DRIFT_PER_ENGAGEMENT = 2

# Label for the engagement-tick disposition beat (ADR-136). The interest tick
# is a small warm drift from continued player attention (ADR-014/ADR-020).
ENGAGEMENT_BEAT_REASON = "warmed by your continued attention"


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

    Increments the interest counter, escalates ``resolution_tier`` per the
    named ladder, and warms ``disposition`` through the clamping constructor.
    Returns the before/after deltas so the caller can emit the development-tick
    and (when disposition actually moved) the ``disposition.shift`` span.
    """
    tier_before = npc.resolution_tier
    disposition_before = int(npc.disposition)
    attitude_before = npc.disposition.attitude().value

    npc.non_transactional_interactions += 1
    npc.resolution_tier = tier_for_interactions(npc.non_transactional_interactions)
    npc.disposition = Disposition(disposition_before + DISPOSITION_DRIFT_PER_ENGAGEMENT)

    return DevelopmentTick(
        interactions=npc.non_transactional_interactions,
        tier_before=tier_before,
        tier_after=npc.resolution_tier,
        disposition_before=disposition_before,
        disposition_after=int(npc.disposition),
        attitude_before=attitude_before,
        attitude_after=npc.disposition.attitude().value,
    )
