"""Story 86-2 (AC5/AC7/AC8): two-pool solo-rig combat, end-to-end + OTEL.

The named integration test from AC8. Drives the production two-pool
resolution through a real ``TracerProvider`` + ``WatcherSpanProcessor``
(same harness as ``tests/integration/test_rig_pool_wiring.py``) and
asserts the GM-panel lie-detector sees the whole chain fire — not
improvised prose:

  Pool 1 (Rig Composure)            Pool 2 (Driver HP)
  ─────────────────────            ──────────────────
  armored hit → rig_pool.delta
  reach 0     → rig_pool.zero_crossing
              → rig_pool.crash_event ──► CWN crash saves
                                          → driver HP loss (half-max-HP
                                            per failed save, NOT the −1
                                            placeholder)
                                          → dismounted (foot-combat
                                            transition)

**Architecture-agnostic by design.** AC7 leaves the routing open ("a new
WinCondition variant OR a beat-application branch"). This test pins the
*behavior + telemetry* every routing must satisfy, not an internal
WinCondition name. The deterministic ``crash_save_outcomes`` injection
stands in for the server-rolled Physical/Luck saves so the test does not
depend on RNG.

Proposed two-pool seam (TEA contract, open to Dev refinement):
    apply_rig_damage(core, amount, *, armor=0,
                     crash_save_outcomes=(physical_passed, luck_passed),
                     location=None, attacker=None) -> RigDamageResult
      — on a hit that destroys the rig, runs the CWN crash saves
        (replacing the legacy −1 ``DRIVER_HP_HIT`` placeholder) and
        carries the realized driver HP delta onto the crash_event span.

RED until Dev wires armor reduction + CWN crash saves into the rig
damage seam. (Note: the legacy 53-x crash tests asserting a flat −1 HP
are expected to migrate in this story, per the design-doc placeholder
replacement — flag, don't treat as a pre-existing pass.)
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


def _mounted_core(
    *,
    name: str = "Mira",
    composure: int = 4,
    composure_max: int = 4,
    driver_hp: int = 8,
    driver_hp_max: int = 8,
    chassis_id: str = "rig_tier_2_road_captain",
):
    from sidequest.game import CreatureCore, HpPool, Inventory, RigComposurePool

    pool = RigComposurePool(
        current=composure,
        max=composure_max,
        base_max=composure_max,
        character_id=name,
        chassis_id=chassis_id,
    )
    return CreatureCore(
        name=name,
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=driver_hp, max=driver_hp_max, base_max=driver_hp_max),
        acquired_advancements=[],
        rig_pool=pool,
    )


async def _setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer(label)
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)
    return captured


def _rig_ops(captured: list[dict]) -> list[str]:
    return [
        e["fields"].get("op")
        for e in captured
        if e.get("event_type") == "state_transition"
        and e.get("component") == "rig"
        and e["fields"].get("op") is not None
    ]


@pytest.mark.asyncio
async def test_rig_two_pool_combat_fires_full_span_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single armored killing hit on a solo rig must publish the full
    rig telemetry chain — delta → zero_crossing → crash_event — through
    the real watcher route, so the GM panel can audit the crash."""
    from sidequest.game.rig_crash import apply_rig_damage

    captured = await _setup(monkeypatch, "test-two-pool-span-chain")

    core = _mounted_core(composure=4, composure_max=4, driver_hp=8, driver_hp_max=8)
    await asyncio.sleep(0.05)
    captured.clear()

    # 6 raw − 2 armor = 4 → exactly destroys a 4-composure rig.
    apply_rig_damage(
        core,
        6,
        armor=2,
        crash_save_outcomes=(False, False),
        location="dust_canyon",
        attacker="raider_chief",
    )
    await asyncio.sleep(0.05)

    ops = _rig_ops(captured)
    assert "delta" in ops, f"rig_pool.delta must fire on the armored hit (got {ops})"
    assert "zero_crossing" in ops, f"rig_pool.zero_crossing must fire at 0 (got {ops})"
    assert "crash_event" in ops, f"rig_pool.crash_event must fire on destruction (got {ops})"


@pytest.mark.asyncio
async def test_two_pool_armor_reduces_then_crosses_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool 1: the delta span must report the *armor-reduced* magnitude
    (6 − 2 = 4), and the hit must cross the 4-composure rig to exactly 0
    — proving Armor filters damage in the live flow, not just in unit
    isolation."""
    from sidequest.game.rig_crash import apply_rig_damage

    captured = await _setup(monkeypatch, "test-two-pool-armor")

    core = _mounted_core(composure=4, composure_max=4)
    await asyncio.sleep(0.05)
    captured.clear()

    apply_rig_damage(core, 6, armor=2, crash_save_outcomes=(False, False))
    await asyncio.sleep(0.05)

    deltas = [
        e
        for e in captured
        if e.get("component") == "rig" and e["fields"].get("op") == "delta"
    ]
    assert len(deltas) == 1, f"expected one rig delta (got {len(deltas)})"
    f = deltas[0]["fields"]
    assert f["delta"] == -4, "delta must be the armor-reduced 4, not the raw 6"
    assert f["old_current"] == 4
    assert f["new_current"] == 0


@pytest.mark.asyncio
async def test_two_pool_crash_damages_driver_pool_not_just_minus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool 2: destroying the rig must transfer harm to the *driver HP
    pool* via CWN crash saves. Both saves fail → half-max + half-max =
    full max HP (8 → 0), NOT the legacy −1 placeholder. This is the
    net-new two-pool transition (AC7)."""
    from sidequest.game.rig_crash import apply_rig_damage

    await _setup(monkeypatch, "test-two-pool-driver-damage")

    core = _mounted_core(composure=4, composure_max=4, driver_hp=8, driver_hp_max=8)

    apply_rig_damage(core, 6, armor=2, crash_save_outcomes=(False, False))
    await asyncio.sleep(0.05)

    # Both saves failed: 8 // 2 + 8 // 2 = 8 → driver at 0. Definitively
    # more than the −1 placeholder, proving the CWN crash-save path ran.
    assert core.hp.current == 0, (
        f"driver HP must take CWN crash-save damage, not −1 (got {core.hp.current})"
    )


@pytest.mark.asyncio
async def test_two_pool_crash_dismounts_driver_to_foot_combat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: a crashed driver is dismounted — the foot-combat transition.
    After the rig is destroyed the driver must carry a ``dismounted``
    status (the marker the confrontation layer reads to switch the driver
    from rig actions to personal-combat actions)."""
    from sidequest.game.rig_crash import DISMOUNTED_STATUS_TEXT, apply_rig_damage

    await _setup(monkeypatch, "test-two-pool-dismount")

    core = _mounted_core(composure=4, composure_max=4, driver_hp=8, driver_hp_max=8)

    apply_rig_damage(core, 6, armor=2, crash_save_outcomes=(True, True))
    await asyncio.sleep(0.05)

    assert any(s.text == DISMOUNTED_STATUS_TEXT for s in core.statuses), (
        "destroyed rig must dismount the driver (foot-combat transition) "
        "regardless of crash-save outcome"
    )


@pytest.mark.asyncio
async def test_two_pool_passed_saves_spare_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second pool is genuinely save-gated: a destroyed rig whose
    driver passes both crash saves loses NO HP (still dismounts). Guards a
    regression that always drains driver HP on crash regardless of the
    saves — which would make the two-save resolution meaningless."""
    from sidequest.game.rig_crash import apply_rig_damage

    await _setup(monkeypatch, "test-two-pool-saved")

    core = _mounted_core(composure=2, composure_max=2, driver_hp=8, driver_hp_max=8)

    apply_rig_damage(core, 2, armor=0, crash_save_outcomes=(True, True))
    await asyncio.sleep(0.05)

    assert core.hp.current == 8, (
        f"driver who passed both crash saves must keep full HP (got {core.hp.current})"
    )
