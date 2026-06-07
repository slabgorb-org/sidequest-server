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
MAJOR_INJURY_STATUS_TEXT = "major_injury"
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


class CrashSaveResult(BaseModel):
    """Outcome of the CWN occupant crash saves (§2.4.8.2).

    Story 86-2. When a rig is destroyed at combat speed each occupant rolls
    two saves; each *failed* save deals half max HP. ``hp_delta`` is the
    realized (clamped) HP change applied to the driver pool — the second of
    the two-pool model. ``mortal`` flags a driver dropped to 0 (Mortally
    Wounded); ``major_injury`` flags the double-failure tier, which also
    records a persistent Major Injury status.
    """

    hp_delta: int
    mortal: bool
    major_injury: bool


def _already_dismounted(core: CreatureCore) -> bool:
    """True iff the core already carries a ``dismounted`` status.

    The handler uses status-list presence rather than a separate flag so
    snapshot reload of a previously-wrecked character is naturally
    idempotent — pydantic round-trips the status list, and a re-run of
    the handler on the reloaded core skips cleanly.
    """
    return any(s.text == DISMOUNTED_STATUS_TEXT for s in core.statuses)


def resolve_crash_saves(
    core: CreatureCore,
    *,
    physical_passed: bool,
    luck_passed: bool,
) -> CrashSaveResult:
    """Resolve the CWN occupant crash saves (§2.4.8.2), applying HP loss.

    Story 86-2. Each *failed* save deals half max HP (``core.hp.max // 2``).
    Both passed → unscathed (0 delta). One failed → half max HP. Both
    failed → half + half (≈ full max HP), Mortally Wounded, and a
    persistent Major Injury status is appended.

    The caller supplies the save outcomes (the server rolls Physical / the
    second save vs the occupant's save target elsewhere — the engine's
    CWN save system has no "Luck" category, so ``luck_passed`` binds to the
    port's chosen second save). The HP loss is applied to ``core.hp`` here;
    ``hp_delta`` is the realized (clamped) change.
    """
    half = core.hp.max // 2
    failed = (0 if physical_passed else 1) + (0 if luck_passed else 1)

    hp_before = core.hp.current
    if failed:
        core.apply_hp_delta(-half * failed)
    hp_after = core.hp.current
    hp_delta = hp_after - hp_before

    major_injury = failed == 2
    if major_injury:
        core.statuses.append(Status(text=MAJOR_INJURY_STATUS_TEXT, severity=StatusSeverity.Scar))
    # Mortally Wounded: both saves failed, or any failure that bottomed the
    # driver out (CWN "may be Mortal" on a single failed save).
    mortal = major_injury or (failed > 0 and hp_after == 0)

    return CrashSaveResult(hp_delta=hp_delta, mortal=mortal, major_injury=major_injury)


def handle_rig_crash(
    core: CreatureCore,
    *,
    location: str | None = None,
    attacker: str | None = None,
    crash_save_outcomes: tuple[bool, bool] | None = None,
) -> RigCrashResult | None:
    """Fire crash consequences on a destroyed rig.

    No-op (returns ``None``) when:
      - ``core.rig_pool`` is ``None`` (foot soldier, no vessel),
      - ``core.rig_pool`` still has composure (not yet wrecked),
      - the character is already dismounted (idempotency guard).

    Otherwise: applies driver HP loss, appends the two crash statuses,
    emits the OTEL ``rig_pool.crash_event`` span, and returns a populated
    :class:`RigCrashResult`.

    Driver HP loss has two modes (Story 86-2 two-pool model):
      - ``crash_save_outcomes=(physical_passed, luck_passed)`` → run the CWN
        crash saves (:func:`resolve_crash_saves`); half max HP per failed
        save. This is the live solo-rig combat path.
      - ``crash_save_outcomes=None`` → the legacy flat ``DRIVER_HP_HIT``
        (−1) placeholder, preserved for callers that have not yet supplied
        save outcomes.
    """
    pool = core.rig_pool
    if pool is None:
        return None
    if not pool.is_destroyed():
        return None
    if _already_dismounted(core):
        return None

    if crash_save_outcomes is not None:
        physical_passed, luck_passed = crash_save_outcomes
        save_result = resolve_crash_saves(
            core, physical_passed=physical_passed, luck_passed=luck_passed
        )
        hp_delta = save_result.hp_delta
        hp_after = core.hp.current
    else:
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
    armor: int = 0,
    crash_save_outcomes: tuple[bool, bool] | None = None,
    location: str | None = None,
    attacker: str | None = None,
) -> RigDamageResult | None:
    """Apply ``amount`` damage to a character's rig, firing crash on zero.

    Returns ``None`` if the character has no rig pool — callers wanting
    direct Edge damage should use the ``apply_damage`` tool. Otherwise
    returns a :class:`RigDamageResult` carrying the pool delta plus a
    populated ``crash`` field iff the delta wrecked the rig.

    ``armor`` (CWN §2.4.8) is subtracted from a connecting hit before it
    reaches the composure pool; a hit always scratches for at least 1 (a
    fully-absorbed hit still removes 1 composure). ``armor`` defaults to 0
    (pass-through), preserving legacy call sites.

    When the armor-reduced hit destroys the rig, ``crash_save_outcomes``
    is forwarded to :func:`handle_rig_crash` — supply
    ``(physical_passed, luck_passed)`` for the live CWN crash-save path
    (the two-pool transition); omit it for the legacy −1 placeholder.

    ``amount`` and ``armor`` must be non-negative. Negative amounts
    (healing) have no legitimate caller here — fail loud rather than
    silently ``abs()``.
    """
    if amount < 0:
        raise ValueError(f"amount must be >= 0, got {amount}")
    if armor < 0:
        raise ValueError(f"armor must be >= 0, got {armor}")

    pool = core.rig_pool
    if pool is None:
        return None

    realized = max(1, amount - armor) if amount > 0 else 0
    pool_result = pool.apply_delta(-realized)

    crash: RigCrashResult | None = None
    if pool_result.zero_crossed:
        crash = handle_rig_crash(
            core,
            location=location,
            attacker=attacker,
            crash_save_outcomes=crash_save_outcomes,
        )

    return RigDamageResult(pool_result=pool_result, crash=crash)


__all__ = [
    "DISMOUNTED_STATUS_TEXT",
    "DRIVER_HP_HIT",
    "INJURY_STATUS_TEXT",
    "MAJOR_INJURY_STATUS_TEXT",
    "CrashSaveResult",
    "RigCrashResult",
    "RigDamageResult",
    "apply_rig_damage",
    "handle_rig_crash",
    "resolve_crash_saves",
]
