"""Rig crash handler — Composure→0 fires injury tag + Edge hit + dismount.

Story 53-3, Epic 53 (Road Warrior). The consequence layer that subscribes
to :class:`~sidequest.game.rig_composure_pool.RigComposurePool` downward
zero-crossing events. When a driver's rig wrecks, four things happen
(per ``sidequest-content/genre_packs/road_warrior/rules.yaml`` —
``crash_event`` and ``rig_composure_spec``):

  1. Driver Edge loses 1.
  2. An ``injury`` status is appended (severity :class:`StatusSeverity.Wound`
     per ADR-080 + the ``injury_system`` rules block — injuries persist
     beyond the scene).
  3. A ``dismounted`` status is appended (severity :class:`StatusSeverity.Scar`
     per the ``dismounted_rules`` block — recovery is "a story arc, not a
     shopping trip").
  4. An OTEL ``rig_pool.crash_event`` span fires with the rig + character
     identifiers plus optional ``location`` / ``attacker`` attrs per
     ADR-031.

:func:`apply_rig_damage` is the production-facing seam combining
:meth:`RigComposurePool.apply_delta` with this handler so downstream
callers (combat resolver, dogfight subsystem) get a single entry point
rather than re-implementing the delta-then-branch pattern at every site.
"""

from __future__ import annotations

from pydantic import BaseModel

from sidequest.game.creature_core import CreatureCore
from sidequest.game.rig_composure_pool import RigComposureDeltaResult
from sidequest.game.status import Status, StatusSeverity
from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT, Span

INJURY_STATUS_TEXT = "injury"
DISMOUNTED_STATUS_TEXT = "dismounted"
DRIVER_HP_HIT = -1


class RigCrashResult(BaseModel):
    """Outcome of a single crash event.

    Carries enough context for the caller to follow up (narrator hook,
    GM-panel breadcrumb) without re-reading the pool — `edge_after` is
    the post-hit driver Edge so a downstream resolver can decide whether
    the driver also went to zero.
    """

    character_id: str
    chassis_id: str
    edge_after: int


class RigDamageResult(BaseModel):
    """Outcome of an :func:`apply_rig_damage` call.

    ``pool_result`` carries the realized old/new composure (clamped) and
    the ``zero_crossed`` edge-trigger flag. ``crash`` is populated iff
    the delta wrecked the rig — sublethal damage and damage to an
    already-wrecked rig both yield ``crash is None``.
    """

    pool_result: RigComposureDeltaResult
    crash: RigCrashResult | None


def _already_dismounted(core: CreatureCore) -> bool:
    """True iff the core already carries a ``dismounted`` status.

    The handler uses status-list presence rather than a separate flag so
    snapshot reload of a previously-wrecked character is naturally
    idempotent — pydantic round-trips the status list, and a re-run of
    the handler on the reloaded core skips cleanly.
    """
    return any(s.text == DISMOUNTED_STATUS_TEXT for s in core.statuses)


def handle_rig_crash(
    core: CreatureCore,
    *,
    location: str | None = None,
    attacker: str | None = None,
) -> RigCrashResult | None:
    """Fire crash consequences on a destroyed rig.

    No-op (returns ``None``) when:
      - ``core.rig_pool`` is ``None`` (foot soldier, no vessel),
      - ``core.rig_pool`` still has composure (not yet wrecked),
      - the character is already dismounted (idempotency guard).

    Otherwise: drains 1 Edge, appends the two crash statuses, emits the
    OTEL ``rig_pool.crash_event`` span, and returns a populated
    :class:`RigCrashResult`.
    """
    pool = core.rig_pool
    if pool is None:
        return None
    if not pool.is_destroyed():
        return None
    if _already_dismounted(core):
        return None

    hp_before = core.hp.current
    core.apply_hp_delta(DRIVER_HP_HIT)
    hp_after = core.hp.current
    # Realized delta — the HpPool floors at 0, so a driver at 0 HP
    # takes the crash but loses no HP; the span reports what actually
    # happened, not what was requested (story 53-4 ADR-031 Layer-2
    # contract: capture what was decided).
    hp_delta = hp_after - hp_before
    core.statuses.append(Status(text=INJURY_STATUS_TEXT, severity=StatusSeverity.Wound))
    core.statuses.append(Status(text=DISMOUNTED_STATUS_TEXT, severity=StatusSeverity.Scar))

    with Span.open(
        SPAN_RIG_POOL_CRASH_EVENT,
        attrs={
            "character_id": pool.character_id,
            "chassis_id": pool.chassis_id,
            "location": location or "",
            "attacker": attacker or "",
            "hp_delta": hp_delta,
            "hp_after": hp_after,
            "injury_status_text": INJURY_STATUS_TEXT,
            "dismounted_status_text": DISMOUNTED_STATUS_TEXT,
        },
    ):
        pass

    return RigCrashResult(
        character_id=pool.character_id,
        chassis_id=pool.chassis_id,
        edge_after=core.hp.current,
    )


def apply_rig_damage(
    core: CreatureCore,
    amount: int,
    *,
    location: str | None = None,
    attacker: str | None = None,
) -> RigDamageResult | None:
    """Apply ``amount`` damage to a character's rig, firing crash on zero.

    Returns ``None`` if the character has no rig pool — callers wanting
    direct Edge damage should use the ``apply_damage`` tool. Otherwise
    returns a :class:`RigDamageResult` carrying the pool delta plus a
    populated ``crash`` field iff the delta wrecked the rig.

    ``amount`` must be non-negative. Negative amounts (healing) have no
    legitimate caller here — fail loud rather than silently ``abs()``.
    """
    if amount < 0:
        raise ValueError(f"amount must be >= 0, got {amount}")

    pool = core.rig_pool
    if pool is None:
        return None

    pool_result = pool.apply_delta(-amount)

    crash: RigCrashResult | None = None
    if pool_result.zero_crossed:
        crash = handle_rig_crash(core, location=location, attacker=attacker)

    return RigDamageResult(pool_result=pool_result, crash=crash)


__all__ = [
    "DISMOUNTED_STATUS_TEXT",
    "DRIVER_HP_HIT",
    "INJURY_STATUS_TEXT",
    "RigCrashResult",
    "RigDamageResult",
    "apply_rig_damage",
    "handle_rig_crash",
]
