"""Vessel-scoped shared Hull + Hull→0 crash fan-out for the crewed War Rig.

Story 86-6. The crewed War Rig generalizes 86-2's solo two-pool model from ONE
occupant to N: the party shares a single Hull, and when that Hull is destroyed
EVERY seated occupant rolls the CWN crash saves against their own ablative HP.

Per the design spec ``2026-06-09-road-warrior-war-rig-crew-spec.md`` §4 (G3/G4):

  - The Hull is **vessel-scoped**, NOT the character-bound
    :class:`~sidequest.game.rig_composure_pool.RigComposurePool` (whose
    ``character_id`` is required and immutable). It reuses the ``rig_pool.*``
    span vocabulary (``delta`` / ``zero_crossing`` / ``crash_event``) keyed by
    ``vessel_id`` so the GM panel renders the crewed Hull exactly as it renders
    the solo rig. A blank ``vessel_id`` fails loud.
  - On Hull→0 the crash cascade **fans 86-2's** :func:`resolve_crash_saves` out
    to each seated occupant (reuse, not reimplement) — half max HP per failed
    save — and marks each occupant dismounted (the foot-combat transition).

Solo-rig combat stays on 86-2's ``RigComposurePool`` / ``apply_rig_damage`` path
(spec §6); this module is the crewed generalization, not a replacement.
"""

from __future__ import annotations

from pydantic import BaseModel, model_validator

from sidequest.game.creature_core import CreatureCore
from sidequest.game.rig_crash import (
    DISMOUNTED_STATUS_TEXT,
    INJURY_STATUS_TEXT,
    resolve_crash_saves,
)
from sidequest.game.status import Status, StatusSeverity
from sidequest.telemetry.spans import (
    SPAN_RIG_POOL_CRASH_EVENT,
    SPAN_RIG_POOL_CREATED,
    SPAN_RIG_POOL_DELTA,
    SPAN_RIG_POOL_ZERO_CROSSING,
    Span,
)


class WarRigHullDeltaResult(BaseModel):
    """Realized result of a single :meth:`WarRigHull.apply_delta` call.

    Mirrors :class:`~sidequest.game.rig_composure_pool.RigComposureDeltaResult`
    so a caller can branch on ``zero_crossed`` identically to the solo path.
    """

    old_current: int
    new_current: int
    zero_crossed: bool


class WarRigHull(BaseModel):
    """A vessel-scoped shared Hull pool for a crewed War Rig.

    ``current`` ∈ ``[0, max]``. Keyed by ``vessel_id`` (NOT a character) so the
    whole crew shares one pool; a blank ``vessel_id`` fails loud rather than
    smuggle crew identity into the OTEL attribution. Emits the ``rig_pool.*``
    span family — keyed by ``vessel_id`` in the ``character_id``/``chassis_id``
    attr slots the GM panel already renders, with ``vessel_id`` also carried
    explicitly for downstream consumers.
    """

    model_config = {"extra": "forbid"}

    current: int
    max: int
    base_max: int
    vessel_id: str

    @model_validator(mode="after")
    def _check_bounds(self) -> WarRigHull:
        if not self.vessel_id.strip():
            raise ValueError("vessel_id cannot be blank")
        if self.max <= 0:
            raise ValueError(f"max must be > 0, got {self.max}")
        if self.current < 0:
            raise ValueError(f"current must be >= 0, got {self.current}")
        if self.current > self.max:
            raise ValueError(f"current ({self.current}) cannot exceed max ({self.max})")
        return self

    def model_post_init(self, __context: object) -> None:
        """Emit ``rig_pool.created`` so the GM panel sees every Hull that enters
        play — round-trip loads fire it too, matching the solo pool's contract."""
        with Span.open(
            SPAN_RIG_POOL_CREATED,
            attrs={
                "character_id": self.vessel_id,
                "chassis_id": self.vessel_id,
                "vessel_id": self.vessel_id,
                "current": self.current,
                "max": self.max,
            },
        ):
            pass

    def apply_delta(self, delta: int) -> WarRigHullDeltaResult:
        """Apply a Hull delta (negative damages, floored at 0; positive repairs,
        capped at max). Emits ``rig_pool.delta`` always and
        ``rig_pool.zero_crossing`` only on a downward crossing to exactly 0."""
        old_current = self.current
        new_current = max(0, min(self.max, old_current + delta))
        self.current = new_current
        zero_crossed = old_current > 0 and new_current == 0

        with Span.open(
            SPAN_RIG_POOL_DELTA,
            attrs={
                "character_id": self.vessel_id,
                "chassis_id": self.vessel_id,
                "vessel_id": self.vessel_id,
                "delta": delta,
                "old_current": old_current,
                "new_current": new_current,
            },
        ):
            pass

        if zero_crossed:
            with Span.open(
                SPAN_RIG_POOL_ZERO_CROSSING,
                attrs={
                    "character_id": self.vessel_id,
                    "chassis_id": self.vessel_id,
                    "vessel_id": self.vessel_id,
                    "old_current": old_current,
                    "new_current": new_current,
                },
            ):
                pass

        return WarRigHullDeltaResult(
            old_current=old_current,
            new_current=new_current,
            zero_crossed=zero_crossed,
        )

    def is_destroyed(self) -> bool:
        return self.current == 0


class WarRigHullDamageResult(BaseModel):
    """Outcome of an :func:`apply_war_rig_hull_damage` call.

    ``pool_result`` carries the realized Hull delta; ``crashed`` is True iff the
    hit wrecked the Hull (and the crash cascade fanned out);
    ``occupants_crashed`` is how many seated occupants the cascade reached.
    """

    pool_result: WarRigHullDeltaResult
    crashed: bool
    occupants_crashed: int


def _crash_occupant(
    occ: CreatureCore,
    *,
    vessel_id: str,
    crash_save_outcomes: tuple[bool, bool] | None,
    location: str | None,
    attacker: str | None,
) -> None:
    """Run one occupant's crash consequences on a destroyed shared Hull.

    Reuses 86-2's :func:`resolve_crash_saves` for the HP loss (half max HP per
    failed save; both passed → unscathed). Every occupant of a wrecked vessel is
    marked injured + dismounted (the foot-combat transition), matching the solo
    crash handler's unconditional status appends. Emits one
    ``rig_pool.crash_event`` per occupant so the GM panel audits each crew
    member's crash exactly as it audits the solo driver's (86-2).
    """
    hp_before = occ.hp.current
    if crash_save_outcomes is not None:
        physical_passed, luck_passed = crash_save_outcomes
        resolve_crash_saves(occ, physical_passed=physical_passed, luck_passed=luck_passed)
    hp_after = occ.hp.current
    hp_delta = hp_after - hp_before

    if not any(s.text == INJURY_STATUS_TEXT for s in occ.statuses):
        occ.statuses.append(Status(text=INJURY_STATUS_TEXT, severity=StatusSeverity.Wound))
    if not any(s.text == DISMOUNTED_STATUS_TEXT for s in occ.statuses):
        occ.statuses.append(Status(text=DISMOUNTED_STATUS_TEXT, severity=StatusSeverity.Scar))

    with Span.open(
        SPAN_RIG_POOL_CRASH_EVENT,
        attrs={
            "character_id": vessel_id,
            "chassis_id": vessel_id,
            "vessel_id": vessel_id,
            "occupant": occ.name,
            "location": location or "",
            "attacker": attacker or "",
            "hp_delta": hp_delta,
            "hp_after": hp_after,
            "injury_status_text": INJURY_STATUS_TEXT,
            "dismounted_status_text": DISMOUNTED_STATUS_TEXT,
        },
    ):
        pass


def apply_war_rig_hull_damage(
    hull: WarRigHull,
    amount: int,
    *,
    armor: int = 0,
    occupants: list[CreatureCore],
    crash_save_outcomes: tuple[bool, bool] | None = None,
    location: str | None = None,
    attacker: str | None = None,
) -> WarRigHullDamageResult:
    """Apply ``amount`` damage to a shared Hull, fanning the crash on Hull→0.

    ``armor`` (CWN §2.4.8) is subtracted from a connecting hit before it reaches
    the Hull; a connecting hit always scratches for at least 1. When the
    armor-reduced hit destroys the Hull, the crash cascade fans
    :func:`resolve_crash_saves` out to **every** occupant — half max HP per
    failed save — and marks each dismounted, emitting the full
    ``rig_pool.delta → zero_crossing → crash_event`` chain.

    ``amount`` and ``armor`` must be non-negative — healing has no legitimate
    caller here, so fail loud rather than silently ``abs()``.
    """
    if amount < 0:
        raise ValueError(f"amount must be >= 0, got {amount}")
    if armor < 0:
        raise ValueError(f"armor must be >= 0, got {armor}")

    realized = max(1, amount - armor) if amount > 0 else 0
    pool_result = hull.apply_delta(-realized)

    crashed = pool_result.zero_crossed
    occupants_crashed = 0
    if crashed:
        for occ in occupants:
            _crash_occupant(
                occ,
                vessel_id=hull.vessel_id,
                crash_save_outcomes=crash_save_outcomes,
                location=location,
                attacker=attacker,
            )
            occupants_crashed += 1

    return WarRigHullDamageResult(
        pool_result=pool_result,
        crashed=crashed,
        occupants_crashed=occupants_crashed,
    )


__all__ = [
    "WarRigHull",
    "WarRigHullDamageResult",
    "WarRigHullDeltaResult",
    "apply_war_rig_hull_damage",
]
