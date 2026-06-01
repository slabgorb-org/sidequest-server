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
                personality_read=None,
                ocean=None,
                claims=[],
            )
        )
    return entries
