"""Story 86-2 (AC2/AC3): Armor reduces rig damage before the pool delta.

CWN §2.4.8: "Vehicles may have an Armor rating subtracted from all
damage." Today ``apply_rig_damage(core, amount)`` applies the raw amount
straight to ``RigComposurePool`` — there is no Armor reduction (53-2's
parser even ignored the ``armor:N`` tag). Plan 2 wires Armor into the
production damage seam.

Proposed seam (TEA contract, open to Dev refinement): ``apply_rig_damage``
gains an ``armor`` keyword; realized composure loss = ``max(1, amount −
armor)`` on a positive hit (a hit always scratches — minimum 1, never 0
and never negative/heal).

These tests are RED until Dev threads Armor into ``apply_rig_damage``.
The pre-existing ``apply_rig_damage`` zero-floor / negative-amount /
crash-on-zero behavior (tests/game/test_rig_crash_handler.py) must be
preserved — Armor reduction layers *in front of* the pool delta.
"""

from __future__ import annotations

from sidequest.game import CreatureCore, HpPool, Inventory, RigComposurePool
from sidequest.game.rig_crash import apply_rig_damage


def _mounted_core(*, composure: int = 6, composure_max: int = 6) -> CreatureCore:
    pool = RigComposurePool(
        current=composure,
        max=composure_max,
        base_max=composure_max,
        character_id="Mira",
        chassis_id="rig_tier_2_road_captain",
    )
    return CreatureCore(
        name="Mira",
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=8, max=8, base_max=8),
        acquired_advancements=[],
        rig_pool=pool,
    )


def test_armor_reduces_realized_composure_loss() -> None:
    """A 5-damage hit against Armor 2 removes 3 composure (5 − 2), not 5."""
    core = _mounted_core(composure=6)
    result = apply_rig_damage(core, 5, armor=2)
    assert result is not None
    assert result.pool_result.new_current == 3
    assert result.pool_result.old_current == 6


def test_armor_reduction_floors_at_one() -> None:
    """A hit that Armor would fully absorb still scratches for 1 — a
    connecting hit never deals 0 (CWN: Armor is subtracted, minimum 1)."""
    core = _mounted_core(composure=6)
    result = apply_rig_damage(core, 2, armor=5)
    assert result is not None
    assert result.pool_result.old_current - result.pool_result.new_current == 1


def test_zero_armor_is_full_damage() -> None:
    """Armor 0 (tier-1 rigs) is the pass-through case — full damage lands,
    identical to the legacy no-armor behavior."""
    core = _mounted_core(composure=6)
    result = apply_rig_damage(core, 4, armor=0)
    assert result is not None
    assert result.pool_result.new_current == 2


def test_armor_default_is_zero_back_compat() -> None:
    """Callers that omit ``armor`` get the legacy full-damage behavior so
    existing call sites and the 53-x tests keep passing."""
    core = _mounted_core(composure=6)
    result = apply_rig_damage(core, 4)
    assert result is not None
    assert result.pool_result.new_current == 2


def test_armor_reduced_hit_can_still_cross_to_zero_and_crash() -> None:
    """Armor lowers the magnitude but a post-armor hit that reaches 0 must
    still fire the crash (zero-crossing → handle_rig_crash). Armor is a
    damage filter, not a crash suppressor."""
    core = _mounted_core(composure=3)
    result = apply_rig_damage(core, 6, armor=2)  # 6 − 2 = 4 ≥ 3 → crosses 0
    assert result is not None
    assert result.pool_result.new_current == 0
    assert result.pool_result.zero_crossed is True
    assert result.crash is not None
