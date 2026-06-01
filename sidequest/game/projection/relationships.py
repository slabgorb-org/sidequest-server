"""Relationship payload builder (ADR-136).

Converts engine NPC state (``snapshot.npcs``) into protocol ``RelationshipEntry``
payloads. Presentation logic (the 5-level display band, trend derivation,
personality read) lives here, keeping the engine ``Attitude`` enum and
``OceanProfile`` untouched. The claims-only firewall (Phase C) is applied here at
build time — disposition/OCEAN/claims are global, so there is no per-recipient
projection fork.
"""

from __future__ import annotations

from sidequest.game.disposition import DispositionBeat

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
