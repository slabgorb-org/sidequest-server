"""Stateless CWN vehicle-combat math (Story 86-2, Epic 86 Road Warrior).

Pure resolution helpers for the solo-rig combat layer — no game state,
no OTEL. The server rolls the d20s (Drive/Dex checks) elsewhere and feeds
the totals in, matching the ``OpponentAttackOutcome`` "caller supplies the
d20" pattern in :mod:`sidequest.game.ruleset.resolution`. Faithful port of
Cities Without Number SRD §2.4.8 (Vehicle Combat); see the design doc
``docs/superpowers/specs/2026-06-04-road-warrior-cwn-rig-combat-design.md``.

Two primitives:

  - :func:`vehicle_ac` — a vehicle's effective AC against incoming attacks.
    Stationary is −4 (a parked rig is easy to hit); moving adds the driver's
    Drive skill modifier (§2.4.8).
  - :func:`resolve_ramming` — the opposed Dex/Drive ram (§2.4.8.4). The
    winner's target takes the ramming vehicle's max HP in damage with
    Trauma 1d12/×3; the ram is mutual (the rammer takes damage back).
"""

from __future__ import annotations

from pydantic import BaseModel

# CWN §2.4.8: a stationary vehicle is −4 to its AC.
STATIONARY_AC_PENALTY = -4

# CWN §2.4.8.4: a ram carries Trauma 1d12 / ×3 onto the target.
RAM_TRAUMA_DIE = "1d12"
RAM_TRAUMA_RATING = 3


def vehicle_ac(base_ac: int, *, drive_modifier: int, moving: bool) -> int:
    """Effective vehicle AC against melee/ranged attacks (CWN §2.4.8).

    Moving → ``base_ac + drive_modifier`` (the driver's skill keeps the rig
    weaving). Stationary → ``base_ac − 4`` (the Drive bonus does not apply
    to a parked rig; the penalty is fixed). The signed ``drive_modifier`` is
    applied as-is — a negative (unskilled) modifier lowers the moving AC.
    """
    if moving:
        return base_ac + drive_modifier
    return base_ac + STATIONARY_AC_PENALTY


class RammingResult(BaseModel):
    """Outcome of one opposed-Dex/Drive ram (CWN §2.4.8.4).

    A win deals the ramming vehicle's max HP to the target with Trauma
    1d12/×3, and is *mutual* — the rammer takes damage back. The exact
    rammed-back figure is a calibration detail (Story 86-2 uses a symmetric
    max-HP hit); the load-bearing invariant the SRD pins is that a winning
    ram damages *both* vehicles.
    """

    model_config = {"extra": "forbid"}

    attacker_wins: bool
    defender_damage: int
    attacker_damage: int
    delivers_trauma: bool
    trauma_die: str
    trauma_rating: int


def resolve_ramming(
    *,
    attacker_total: int,
    defender_total: int,
    attacker_max_hp: int,
    defender_max_hp: int,
) -> RammingResult:
    """Resolve an opposed Dex/Drive ram from pre-rolled totals.

    The attacker must *beat* the defender's opposed total (a tie does not
    connect). On a win the target takes the ramming vehicle's max HP in
    damage plus Trauma 1d12/×3, and the rammer takes a mutual hit back. On a
    loss/tie no ram damage is dealt and no trauma is carried.

    ``defender_max_hp`` is accepted for symmetry / future calibration of the
    rammed-back figure; the current symmetric model deals the attacker's max
    HP to both sides.
    """
    attacker_wins = attacker_total > defender_total
    if attacker_wins:
        return RammingResult(
            attacker_wins=True,
            defender_damage=attacker_max_hp,
            attacker_damage=attacker_max_hp,
            delivers_trauma=True,
            trauma_die=RAM_TRAUMA_DIE,
            trauma_rating=RAM_TRAUMA_RATING,
        )
    return RammingResult(
        attacker_wins=False,
        defender_damage=0,
        attacker_damage=0,
        delivers_trauma=False,
        trauma_die=RAM_TRAUMA_DIE,
        trauma_rating=RAM_TRAUMA_RATING,
    )


__all__ = [
    "RAM_TRAUMA_DIE",
    "RAM_TRAUMA_RATING",
    "STATIONARY_AC_PENALTY",
    "RammingResult",
    "resolve_ramming",
    "vehicle_ac",
]
