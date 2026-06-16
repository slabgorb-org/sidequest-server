"""Story 86-2 (AC4): CWN crash saves apply half-max-HP per failed save.

CWN Vehicle Combat §2.4.8.2 (design doc §4.1), crash at combat speed —
each occupant rolls two saves:

    both pass  → unscathed
    fail one   → half max HP damage (may be Mortal)
    fail both  → Mortally Wounded + Major Injury if survived

Today ``handle_rig_crash`` applies a flat ``DRIVER_HP_HIT = −1`` — a
placeholder, NOT the CWN save-based crash damage. Plan 2 replaces that
with the real two-save resolution.

**Faithful-port note (TEA → Dev):** the design doc names the saves
"Physical + Luck", but the engine's SWN/CWN save system has only
Physical / Evasion / Mental — there is no "Luck" save category. The
Physical/Luck → Physical/Evasion mapping is a Dev port decision. These
tests therefore pin *outcomes* (damage magnitude + mortal/major-injury
flags), not a save-category name. The second save param is named
``luck_passed`` to match the SRD wording; Dev binds it to whichever
category the port chooses.

Proposed seam (TEA contract, open to refinement):
    resolve_crash_saves(core, *, physical_passed: bool, luck_passed: bool)
        -> CrashSaveResult(hp_delta: int, mortal: bool, major_injury: bool)
    — applies the realized HP loss to ``core.hp`` and appends a
      major-injury status on a double failure.

RED until Dev implements the CWN crash-save resolver.
"""

from __future__ import annotations

from sidequest.game import CreatureCore, HpPool, Inventory, RigComposurePool
from sidequest.game.rig_crash import resolve_crash_saves


def _occupant(*, hp: int = 8, hp_max: int = 8) -> CreatureCore:
    pool = RigComposurePool(
        current=0,  # rig already wrecked — the crash is happening
        max=6,
        base_max=6,
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
        hp=HpPool(current=hp, max=hp_max, base_max=hp_max),
        acquired_advancements=[],
        rig_pool=pool,
    )


def test_both_saves_pass_unscathed() -> None:
    """Both saves pass → no HP loss, not mortal, no major injury (CWN: the
    occupant rides out the crash)."""
    core = _occupant(hp=8, hp_max=8)
    result = resolve_crash_saves(core, physical_passed=True, luck_passed=True)
    assert result.hp_delta == 0
    assert core.hp.current == 8
    assert result.mortal is False
    assert result.major_injury is False


def test_one_failed_save_deals_half_max_hp() -> None:
    """Failing exactly one save deals half max HP (8 // 2 = 4) — the
    occupant drops from 8 to 4."""
    core = _occupant(hp=8, hp_max=8)
    result = resolve_crash_saves(core, physical_passed=False, luck_passed=True)
    assert result.hp_delta == -4
    assert core.hp.current == 4
    assert result.major_injury is False


def test_either_single_failure_is_symmetric() -> None:
    """Failing the *other* save alone deals the same half-max-HP — the two
    saves are symmetric in damage (the SRD does not privilege one)."""
    core = _occupant(hp=8, hp_max=8)
    result = resolve_crash_saves(core, physical_passed=True, luck_passed=False)
    assert result.hp_delta == -4
    assert core.hp.current == 4


def test_both_saves_fail_is_mortal_with_major_injury() -> None:
    """Failing both saves is the worst tier: half-max-HP *per* failed save
    (4 + 4 = 8 → 0 HP), flagged mortal, with a Major Injury recorded."""
    core = _occupant(hp=8, hp_max=8)
    result = resolve_crash_saves(core, physical_passed=False, luck_passed=False)
    assert core.hp.current == 0
    assert result.mortal is True
    assert result.major_injury is True
    assert any("injury" in s.text for s in core.statuses)


def test_double_failure_is_strictly_worse_than_single() -> None:
    """A double failure must remove strictly more HP than a single failure
    from the same starting pool — guards a regression that caps crash
    damage at one half-max chunk regardless of saves."""
    one = _occupant(hp=10, hp_max=10)
    both = _occupant(hp=10, hp_max=10)
    one_res = resolve_crash_saves(one, physical_passed=False, luck_passed=True)
    both_res = resolve_crash_saves(both, physical_passed=False, luck_passed=False)
    assert abs(both_res.hp_delta) > abs(one_res.hp_delta)
