"""Command Points economy + d10 Crisis table for the crewed War Rig (Story 86-7).

The SWN §4.3 *command* crunch layered on 86-6's War Rig table game. Two pieces:

  - **Command Points** — a per-vessel SHARED resource (:class:`CommandPointPool`).
    Three actions: ``do_your_duty`` (free, standard resolution), ``above_and_beyond``
    (1 CP, +1d6), ``support_department`` (1 CP, assist another station). Spending
    more than the pool holds, or an unknown action, FAILS LOUD (No Silent
    Fallbacks) — a crew cannot conjure command out of nothing, and the narrator
    cannot claim a bonus the engine never granted.

  - **Crisis table** — a d10 table (:data:`CRISIS_TABLE`) of ``continuing`` (rolls
    each round, escalating) and ``acute`` (one-round) crises. :func:`deal_with_crisis`
    rolls d10 + ability vs the crisis DC; a failed *continuing* crisis escalates and,
    **when a Hull is supplied**, feeds its hull penalty into 86-2's two-pool model via
    :meth:`~sidequest.game.war_rig_combat.WarRigHull.apply_delta` (reuse, not
    reimplement). The ``war_rig_crew`` round supplies the crew's shared Hull, so this
    fires in live play (see ``table.war_rig.custom_beat``); with no Hull the escalation
    still emits ``crisis.escalated`` but deducts no HP. An *acute* crisis does not continue.

Mirrors :mod:`sidequest.game.war_rig_combat`'s shape: a vessel-scoped pydantic pool
(blank ``vessel_id`` fails loud) + pure resolvers that emit OTEL on every decision so
the GM panel can audit the command economy (the lie-detector mandate).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, model_validator

from sidequest.game.war_rig_combat import WarRigHull
from sidequest.telemetry.spans import (
    emit_command_points_action_taken,
    emit_command_points_delta,
    emit_crisis_escalated,
    emit_crisis_resolved,
    emit_crisis_rolled,
)

# --- Command Points -------------------------------------------------------

CP_DO_YOUR_DUTY = "do_your_duty"
CP_ABOVE_AND_BEYOND = "above_and_beyond"
CP_SUPPORT_DEPARTMENT = "support_department"

# SWN §4.3: Do Your Duty is the free standard action; the other two each cost 1 CP.
CP_ACTION_COSTS: dict[str, int] = {
    CP_DO_YOUR_DUTY: 0,
    CP_ABOVE_AND_BEYOND: 1,
    CP_SUPPORT_DEPARTMENT: 1,
}


class CommandPointPool(BaseModel):
    """A vessel-scoped SHARED Command Point pool for a crewed War Rig.

    ``current`` ∈ ``[0, max]``. Keyed by ``vessel_id`` (NOT a seat) so the whole
    crew draws from one pool; a blank ``vessel_id`` fails loud, matching
    :class:`~sidequest.game.war_rig_combat.WarRigHull`.
    """

    model_config = {"extra": "forbid"}

    current: int
    max: int
    vessel_id: str

    @model_validator(mode="after")
    def _check_bounds(self) -> CommandPointPool:
        if not self.vessel_id.strip():
            raise ValueError("vessel_id cannot be blank")
        if self.max <= 0:
            raise ValueError(f"max must be > 0, got {self.max}")
        if self.current < 0:
            raise ValueError(f"current must be >= 0, got {self.current}")
        if self.current > self.max:
            raise ValueError(f"current ({self.current}) cannot exceed max ({self.max})")
        return self


@dataclass(frozen=True)
class CommandPointSpendResult:
    """Realized result of a single :func:`spend_command_points` call."""

    action: str
    cost: int
    bonus: int
    old_current: int
    new_current: int


def spend_command_points(
    pool: CommandPointPool,
    action: str,
    *,
    seat: str,
    rng: random.Random,
) -> CommandPointSpendResult:
    """Spend a CP action against the shared pool (mutates ``pool`` in place).

    Fails loud on an unknown action or on a spend the pool can't afford (No
    Silent Fallbacks). ``above_and_beyond`` rolls a +1d6 bonus. Emits
    ``command_points.action_taken`` for every action and ``command_points.delta``
    only when the pool actually changes (cost>0).
    """
    if action not in CP_ACTION_COSTS:
        raise ValueError(
            f"unknown command-point action {action!r} (known: {sorted(CP_ACTION_COSTS)})"
        )
    cost = CP_ACTION_COSTS[action]
    if cost > pool.current:
        raise ValueError(
            f"insufficient command points for {action!r}: need {cost}, have {pool.current}"
        )

    bonus = rng.randint(1, 6) if action == CP_ABOVE_AND_BEYOND else 0
    old_current = pool.current
    pool.current = old_current - cost
    new_current = pool.current

    emit_command_points_action_taken(
        vessel_id=pool.vessel_id, seat=seat, action=action, cost=cost, bonus=bonus
    )
    if cost > 0:
        emit_command_points_delta(
            vessel_id=pool.vessel_id,
            seat=seat,
            action=action,
            delta=-cost,
            old_current=old_current,
            new_current=new_current,
        )

    return CommandPointSpendResult(
        action=action,
        cost=cost,
        bonus=bonus,
        old_current=old_current,
        new_current=new_current,
    )


# --- Crisis table ---------------------------------------------------------

CrisisType = Literal["continuing", "acute"]


class CrisisEntry(BaseModel):
    """One face of the d10 Crisis table.

    ``hull_penalty`` is applied to the shared Hull when a *continuing* crisis is
    failed **and a Hull is passed to** :func:`deal_with_crisis` (escalation feeds
    86-2's two-pool model). It is inert for an *acute* crisis (a one-round threat that
    does not continue) and inert when no Hull is supplied (the ``war_rig_crew`` round
    supplies one; bare callers that omit it still escalate but deduct no HP).
    """

    model_config = {"extra": "forbid"}

    crisis_id: str
    crisis_type: CrisisType
    dc: int
    description: str
    hull_penalty: int


# Minimal playable d10 table (spec §6 — sufficient to prove the wiring; full
# lethality calibration is 86-5). Both crisis kinds are represented so the
# escalation and one-round branches are both reachable in play.
CRISIS_TABLE: dict[int, CrisisEntry] = {
    1: CrisisEntry(
        crisis_id="loose_cargo",
        crisis_type="acute",
        dc=8,
        description="A strap snaps; the load lurches and fouls a wheel well.",
        hull_penalty=1,
    ),
    2: CrisisEntry(
        crisis_id="blinding_dust",
        crisis_type="acute",
        dc=9,
        description="A dust devil swallows the road; the driver is flying blind.",
        hull_penalty=1,
    ),
    3: CrisisEntry(
        crisis_id="seized_gun",
        crisis_type="acute",
        dc=9,
        description="The turret jams mid-burst — brass cooks in the breech.",
        hull_penalty=1,
    ),
    4: CrisisEntry(
        crisis_id="fuel_leak",
        crisis_type="continuing",
        dc=10,
        description="A fuel line weeps onto the hot manifold. It will get worse.",
        hull_penalty=1,
    ),
    5: CrisisEntry(
        crisis_id="engine_fire",
        crisis_type="continuing",
        dc=11,
        description="Flames lick from under the hood and spread toward the cab.",
        hull_penalty=2,
    ),
    6: CrisisEntry(
        crisis_id="shredded_tire",
        crisis_type="continuing",
        dc=10,
        description="A tire blows; the rig drags and the rim chews the road.",
        hull_penalty=1,
    ),
    7: CrisisEntry(
        crisis_id="boarders",
        crisis_type="continuing",
        dc=12,
        description="Raiders grapple the flank and start cutting their way in.",
        hull_penalty=2,
    ),
    8: CrisisEntry(
        crisis_id="steering_fault",
        crisis_type="continuing",
        dc=11,
        description="The wheel goes soft — the linkage is tearing itself apart.",
        hull_penalty=1,
    ),
    9: CrisisEntry(
        crisis_id="overheat",
        crisis_type="continuing",
        dc=10,
        description="The temp needle buries itself; the block is cooking.",
        hull_penalty=1,
    ),
    10: CrisisEntry(
        crisis_id="rollover_threat",
        crisis_type="continuing",
        dc=13,
        description="A hard camber and a full load — the rig threatens to roll.",
        hull_penalty=3,
    ),
}


@dataclass(frozen=True)
class CrisisRollResult:
    """A rolled crisis: the d10 face and the table entry it selected."""

    roll: int
    entry: CrisisEntry


def roll_crisis(rng: random.Random, *, vessel_id: str) -> CrisisRollResult:
    """Roll a d10 against the Crisis table and emit ``crisis.rolled``."""
    roll = rng.randint(1, 10)
    entry = CRISIS_TABLE[roll]
    emit_crisis_rolled(
        vessel_id=vessel_id,
        roll=roll,
        crisis_id=entry.crisis_id,
        crisis_type=entry.crisis_type,
        dc=entry.dc,
    )
    return CrisisRollResult(roll=roll, entry=entry)


@dataclass(frozen=True)
class CrisisResolutionResult:
    """Outcome of a :func:`deal_with_crisis` attempt."""

    crisis_id: str
    success: bool
    roll: int
    total: int
    escalated: bool
    hull_delta: int


def deal_with_crisis(
    entry: CrisisEntry,
    *,
    seat: str,
    ability_mod: int,
    rng: random.Random,
    vessel_id: str,
    hull: WarRigHull | None = None,
) -> CrisisResolutionResult:
    """Resolve a Deal With a Crisis attempt: d10 + ability vs the crisis DC.

    Success resolves the crisis. A failed *continuing* crisis escalates and
    ``crisis.escalated`` fires; if a ``hull`` is supplied, its ``hull_penalty`` is
    applied to that shared Hull (reusing 86-2's ``rig_pool.delta`` channel) and the
    realized ``hull_delta`` is reported — if ``hull`` is None the escalation fires but
    deducts no HP (``hull_delta`` 0). The ``war_rig_crew`` round always supplies the
    crew's shared Hull. A failed *acute* crisis is a one-round threat: it does NOT
    escalate and does NOT damage the Hull. Emits ``crisis.resolved`` on every attempt.
    """
    roll = rng.randint(1, 10)
    total = roll + ability_mod
    success = total >= entry.dc

    escalated = False
    hull_delta = 0
    if not success and entry.crisis_type == "continuing":
        escalated = True
        if hull is not None:
            hull.apply_delta(-entry.hull_penalty)
            hull_delta = -entry.hull_penalty
        emit_crisis_escalated(
            vessel_id=vessel_id, seat=seat, crisis_id=entry.crisis_id, hull_delta=hull_delta
        )

    emit_crisis_resolved(
        vessel_id=vessel_id,
        seat=seat,
        crisis_id=entry.crisis_id,
        roll=roll,
        total=total,
        success=success,
    )

    return CrisisResolutionResult(
        crisis_id=entry.crisis_id,
        success=success,
        roll=roll,
        total=total,
        escalated=escalated,
        hull_delta=hull_delta,
    )


__all__ = [
    "CP_ABOVE_AND_BEYOND",
    "CP_ACTION_COSTS",
    "CP_DO_YOUR_DUTY",
    "CP_SUPPORT_DEPARTMENT",
    "CRISIS_TABLE",
    "CommandPointPool",
    "CommandPointSpendResult",
    "CrisisEntry",
    "CrisisResolutionResult",
    "CrisisRollResult",
    "CrisisType",
    "deal_with_crisis",
    "roll_crisis",
    "spend_command_points",
]
