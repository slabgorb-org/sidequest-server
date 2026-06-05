"""Stateless CWN vehicle-chase pace/pursuit math (Story 86-3, Epic 86).

Faithful port of Cities Without Number SRD §2.6.2 (Chases and Pursuit), as
extracted in the design doc §4.2
(``docs/superpowers/specs/2026-06-04-road-warrior-cwn-rig-combat-design.md``).
The pure helpers carry no game state and no OTEL, matching the
:mod:`sidequest.game.vehicle_combat` "caller supplies the d20" pattern. The
one stateful seam, :func:`resolve_chase_round`, drives those helpers and
emits the ``chase.pursuit_resolved`` span so the GM panel can audit each
chase beat rather than trusting improvised prose.

The §2.6.2 loop:

  - The fleeing driver rolls Drive (usually +Dex); that total IS the pace.
  - Passengers may hinder pursuit (skill checks, +1 each, capped at +3).
  - Each pursuer rolls Dex/Drive vs the pace with situational modifiers.
  - Beat the pace → catch up (→ vehicle combat). Tie or under → escape.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

# CWN §2.6.2 situational modifiers, applied to the pursuer's roll vs the
# pace. Signs are load-bearing: a positive value helps the pursuer close.
PURSUER_CANNOT_SEE = -2
PURSUER_FLYING_PURSUED_NOT = 3
PURSUED_FLYING_PURSUER_NOT = -3
SPOTTER_RELAYING = 1
HALF_HEARTED_PURSUIT = -1
ENRAGED_VENGEFUL = 1
TERRAIN_KNOWLEDGE_MIN = -2
TERRAIN_KNOWLEDGE_MAX = 2

# Passengers may hinder pursuit, +1 each, capped at +3 (§2.6.2).
MAX_HINDER = 3


def chase_check_total(*, d20: int, attribute_modifier: int, drive_skill: int) -> int:
    """A chase Drive check total: d20 + attribute modifier (usually Dex) +
    Drive skill (§2.6.2, "Drive, usually +Dex"). Both the flee pace and the
    pursuer roll are built this way."""
    return d20 + attribute_modifier + drive_skill


def hinder_penalty(passenger_successes: int) -> int:
    """Passenger hindrance to pursuit: +1 per success, capped at +3 (§2.6.2).

    A negative success count is a caller bug, surfaced loudly rather than
    silently treated as 0 (No Silent Fallbacks).
    """
    if passenger_successes < 0:
        raise ValueError(f"passenger_successes must be >= 0, got {passenger_successes}")
    return min(passenger_successes, MAX_HINDER)


class PursuitOutcome(StrEnum):
    """A chase round's outcome (§2.6.2)."""

    CAUGHT = "caught"  # pursuer beat the pace → close into vehicle combat
    EVADED = "evaded"  # tie or under → fall behind / escape


class PursuitResult(BaseModel):
    """One pursuer's resolution against the fleeing pace (§2.6.2)."""

    model_config = {"extra": "forbid"}

    outcome: PursuitOutcome
    pursuer_effective: int
    pace: int
    converges_to_combat: bool


def resolve_pursuit(
    *,
    pursuer_total: int,
    pace: int,
    situational_modifier: int = 0,
    hinder: int = 0,
) -> PursuitResult:
    """Resolve one pursuer's roll against the fleeing pace (§2.6.2).

    The pursuer's effective roll is ``pursuer_total + situational_modifier −
    hinder``. *Strictly* beating the pace catches the quarry (CAUGHT) and
    converges into vehicle combat; a tie or under is an EVADE.
    """
    effective = pursuer_total + situational_modifier - hinder
    caught = effective > pace
    return PursuitResult(
        outcome=PursuitOutcome.CAUGHT if caught else PursuitOutcome.EVADED,
        pursuer_effective=effective,
        pace=pace,
        converges_to_combat=caught,
    )


class ChaseRoundResult(BaseModel):
    """The outcome of one resolved chase round, including who was involved."""

    model_config = {"extra": "forbid"}

    outcome: PursuitOutcome
    pace: int
    pursuer_effective: int
    converges_to_combat: bool
    fleeing_id: str
    pursuer_id: str
    location: str | None = None


def resolve_chase_round(
    *,
    fleeing_drive_total: int,
    pursuer_total: int,
    situational_modifier: int = 0,
    passenger_hinder_successes: int = 0,
    fleeing_id: str,
    pursuer_id: str,
    location: str | None = None,
) -> ChaseRoundResult:
    """Resolve a full chase round and emit ``chase.pursuit_resolved``.

    ``fleeing_drive_total`` is the fleeing driver's Drive(+Dex) check total —
    the pace. The pursuer rolls against it with situational modifiers and
    passenger hindrance. Emits one ``chase.pursuit_resolved`` span (the
    GM-panel lie detector) and reports whether the round converges into
    vehicle combat (§2.6.2 "→ vehicle combat", the Plan 2 hand-off).
    """
    from sidequest.telemetry.spans import emit_chase_pursuit_resolved

    result = resolve_pursuit(
        pursuer_total=pursuer_total,
        pace=fleeing_drive_total,
        situational_modifier=situational_modifier,
        hinder=hinder_penalty(passenger_hinder_successes),
    )
    emit_chase_pursuit_resolved(
        pace=result.pace,
        pursuer_effective=result.pursuer_effective,
        outcome=result.outcome.value,
        converges_to_combat=result.converges_to_combat,
        fleeing_id=fleeing_id,
        pursuer_id=pursuer_id,
        location=location,
    )
    return ChaseRoundResult(
        outcome=result.outcome,
        pace=result.pace,
        pursuer_effective=result.pursuer_effective,
        converges_to_combat=result.converges_to_combat,
        fleeing_id=fleeing_id,
        pursuer_id=pursuer_id,
        location=location,
    )


__all__ = [
    "ENRAGED_VENGEFUL",
    "HALF_HEARTED_PURSUIT",
    "MAX_HINDER",
    "PURSUED_FLYING_PURSUER_NOT",
    "PURSUER_CANNOT_SEE",
    "PURSUER_FLYING_PURSUED_NOT",
    "SPOTTER_RELAYING",
    "TERRAIN_KNOWLEDGE_MAX",
    "TERRAIN_KNOWLEDGE_MIN",
    "ChaseRoundResult",
    "PursuitOutcome",
    "PursuitResult",
    "chase_check_total",
    "hinder_penalty",
    "resolve_chase_round",
    "resolve_pursuit",
]
