"""Story 86-6 (RED): War Rig crewed-vessel combat — shared Hull + crash fan-out + OTEL.

The crewed War Rig generalizes 86-2's solo two-pool model from ONE occupant to
N: the party shares a single Hull (the enemy's, and their own), and when a Hull
is destroyed EVERY seated occupant rolls the CWN crash saves against their own
ablative HP. Per the design spec
``docs/superpowers/specs/2026-06-09-road-warrior-war-rig-crew-spec.md`` this
RIDES the ADR-129 table engine; the Hull is vessel-scoped (NOT the
character-bound ``RigComposurePool``, whose ``character_id`` is required and
immutable) and reuses the ``rig_pool.*`` span vocabulary keyed by ``vessel_id``.

Pins gaps G3 (shared Hull pool + spans), G4 (crash fan-out), and the MANDATORY
OTEL wiring test (the GM panel is the lie detector — CLAUDE.md OTEL principle).
The harness is the real ``TracerProvider`` + ``WatcherSpanProcessor`` +
``watcher_hub`` route, identical to ``tests/integration/test_rig_two_pool_combat.py``.

**Proposed seam (TEA contract, open to Dev refinement — mirrors 86-2's
``apply_rig_damage``):**

    sidequest.game.war_rig_combat:
      WarRigHull(current, max, base_max, vessel_id)          # vessel-scoped pool
      apply_war_rig_hull_damage(
          hull, amount, *, armor=0,
          occupants: list[CreatureCore],          # all seated crew
          crash_save_outcomes=(physical_passed, luck_passed),  # deterministic in test
          location=None, attacker=None,
      ) -> result
        — armor-reduces the hit, emits rig_pool.delta; on Hull→0 emits
          rig_pool.zero_crossing, then FANS resolve_crash_saves() across every
          occupant (half-max-HP per failed save) and marks each dismounted,
          emitting rig_pool.crash_event. Architecture-agnostic on storage; this
          file pins the BEHAVIOUR + TELEMETRY any implementation must satisfy.

RED until Dev builds the vessel-scoped Hull + the fan-out seam. Imports live
inside the test bodies so a missing module fails THESE tests, not collection.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


async def _setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    """Bind a fresh watcher subscriber + local tracer; return the captured stream.

    Same harness as test_rig_two_pool_combat.py so the assertions exercise the
    real GM-panel route, not a stub.
    """
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


def _ops(captured: list[dict], component: str) -> list[str]:
    return [
        e["fields"].get("op")
        for e in captured
        if e.get("event_type") == "state_transition"
        and e.get("component") == component
        and e["fields"].get("op") is not None
    ]


def _occupant(name: str, *, hp: int = 8, hp_max: int = 8):
    from sidequest.game import CreatureCore, HpPool, Inventory

    return CreatureCore(
        name=name,
        description="Crew.",
        personality="Grim.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=hp, max=hp_max, base_max=hp_max),
        acquired_advancements=[],
        rig_pool=None,  # crew share ONE Hull — they do not each carry a rig_pool
    )


def _hull(*, current: int = 6, vessel_id: str = "war_rig_alpha"):
    from sidequest.game.war_rig_combat import WarRigHull

    return WarRigHull(current=current, max=current, base_max=current, vessel_id=vessel_id)


# ---------------------------------------------------------------------------
# G3 — shared Hull pool emits rig_pool.* spans (vessel-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sublethal_hull_hit_emits_rig_pool_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-killing hit on the shared Hull publishes rig_pool.delta through the
    real watcher route, with the armor-reduced magnitude — proving the Hull
    reuses the rig telemetry the GM panel already renders."""
    from sidequest.game.war_rig_combat import apply_war_rig_hull_damage

    captured = await _setup(monkeypatch, "war-rig-hull-delta")
    hull = _hull(current=6)
    occupants = [_occupant("Mira")]
    await asyncio.sleep(0.05)
    captured.clear()

    # 4 raw − 1 armor = 3 → 6 → 3, sublethal (no crash).
    apply_war_rig_hull_damage(hull, 4, armor=1, occupants=occupants)
    await asyncio.sleep(0.05)

    rig = [e for e in captured if e.get("component") == "rig" and e["fields"].get("op") == "delta"]
    assert len(rig) == 1, f"exactly one rig_pool.delta expected on a sublethal hit (got {len(rig)})"
    f = rig[0]["fields"]
    assert f["delta"] == -3, f"delta must be the armor-reduced 3, not raw 4 (got {f['delta']})"
    ops = _ops(captured, "rig")
    assert "zero_crossing" not in ops, "a sublethal hit must NOT cross zero"


@pytest.mark.asyncio
async def test_hull_destruction_fires_full_rig_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MANDATORY OTEL wiring test (rig half). A killing hit on the shared Hull
    must publish the full rig telemetry chain — delta → zero_crossing →
    crash_event — through the real WatcherSpanProcessor route, so the GM panel
    can audit the crewed crash exactly as it audits the solo crash (86-2)."""
    from sidequest.game.war_rig_combat import apply_war_rig_hull_damage

    captured = await _setup(monkeypatch, "war-rig-hull-chain")
    hull = _hull(current=4)
    occupants = [_occupant("Mira"), _occupant("Cole"), _occupant("Vex")]
    await asyncio.sleep(0.05)
    captured.clear()

    apply_war_rig_hull_damage(
        hull,
        6,
        armor=2,  # 6 − 2 = 4 → exactly destroys the 4-Hull
        occupants=occupants,
        crash_save_outcomes=(False, False),
        location="salt_flats",
        attacker="war_party",
    )
    await asyncio.sleep(0.05)

    ops = _ops(captured, "rig")
    assert "delta" in ops, f"rig_pool.delta must fire on the hit (got {ops})"
    assert "zero_crossing" in ops, f"rig_pool.zero_crossing must fire at 0 (got {ops})"
    assert "crash_event" in ops, f"rig_pool.crash_event must fire on destruction (got {ops})"
    # The crash must fan to EVERY occupant — one crash_event per crew member, not
    # just one. A membership check would pass even if the fan-out reached only one
    # occupant; pin the count so a partial fan-out is caught (review 86-6).
    crash_events = [o for o in ops if o == "crash_event"]
    assert len(crash_events) == len(occupants), (
        f"rig_pool.crash_event must fire once per occupant ({len(occupants)}), "
        f"got {len(crash_events)} (ops: {ops})"
    )


# ---------------------------------------------------------------------------
# G4 — Hull→0 crash fan-out to N occupants (reuses 86-2 resolve_crash_saves)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hull_destruction_fans_crash_saves_to_every_occupant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crewed generalization of the 86-2 two-pool transition: when the
    shared Hull is destroyed, EVERY seated occupant takes CWN crash-save HP
    loss (not just one driver). Both saves fail → half-max + half-max = full
    max HP, so each 8-HP occupant drops to 0 — definitively the crash-save
    path, not a flat −1 placeholder."""
    from sidequest.game.war_rig_combat import apply_war_rig_hull_damage

    await _setup(monkeypatch, "war-rig-fanout")
    hull = _hull(current=4)
    occupants = [_occupant(n, hp=8, hp_max=8) for n in ("Mira", "Cole", "Vex")]

    apply_war_rig_hull_damage(
        hull,
        6,
        armor=2,
        occupants=occupants,
        crash_save_outcomes=(False, False),
    )
    await asyncio.sleep(0.05)

    for occ in occupants:
        assert occ.hp.current == 0, (
            f"{occ.name} must take CWN crash-save damage on the shared-Hull kill "
            f"(got {occ.hp.current}); the fan-out must reach every occupant"
        )
        assert any(s.text == "dismounted" for s in occ.statuses), (
            f"{occ.name} must be marked 'dismounted' (foot-combat transition) after the crash"
        )


@pytest.mark.asyncio
async def test_passed_saves_spare_the_occupant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative case: an occupant who passes BOTH crash saves takes zero HP
    loss — the fan-out must honour per-occupant save outcomes, not blanket-
    damage the crew."""
    from sidequest.game.war_rig_combat import apply_war_rig_hull_damage

    await _setup(monkeypatch, "war-rig-saved")
    hull = _hull(current=4)
    occupants = [_occupant("Lucky", hp=8, hp_max=8)]

    apply_war_rig_hull_damage(
        hull,
        6,
        armor=2,
        occupants=occupants,
        crash_save_outcomes=(True, True),  # both saves pass → unscathed
    )
    await asyncio.sleep(0.05)

    assert occupants[0].hp.current == 8, (
        f"an occupant who passes both saves keeps full HP (got {occupants[0].hp.current})"
    )


@pytest.mark.asyncio
async def test_armor_exceeding_damage_still_scratches_one_hull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CWN 'a connecting hit always scratches': armor >= raw damage still removes
    1 Hull (``max(1, amount - armor)``), never 0 — mirrors 86-2's
    ``apply_rig_damage`` floor. Pins the distinct armor-over-damage path."""
    from sidequest.game.war_rig_combat import apply_war_rig_hull_damage

    captured = await _setup(monkeypatch, "war-rig-armor-floor")
    hull = _hull(current=6)
    occupants = [_occupant("Mira")]
    await asyncio.sleep(0.05)
    captured.clear()

    # armor 9 >> raw 3 → would be -6, floored to a 1-point scratch (no crash).
    apply_war_rig_hull_damage(hull, 3, armor=9, occupants=occupants)
    await asyncio.sleep(0.05)

    assert hull.current == 5, (
        f"an armor-over-damage hit must still scratch exactly 1 (6→5), got {hull.current}"
    )
    rig = [e for e in captured if e.get("component") == "rig" and e["fields"].get("op") == "delta"]
    assert len(rig) == 1 and rig[0]["fields"]["delta"] == -1, (
        "the scratch must publish a single rig_pool.delta of -1, got "
        f"{[r['fields'].get('delta') for r in rig]}"
    )
    assert "zero_crossing" not in _ops(captured, "rig"), "a 1-point scratch must not cross zero"


@pytest.mark.asyncio
async def test_one_failed_save_costs_exactly_half_max_hp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-occupant save granularity: an occupant who passes ONE crash save and
    fails the other takes exactly half max HP — distinguishing the one-fail path
    from both-pass (0 loss) and both-fail (full max loss)."""
    from sidequest.game.war_rig_combat import apply_war_rig_hull_damage

    await _setup(monkeypatch, "war-rig-one-save")
    hull = _hull(current=4)
    occ = _occupant("Half", hp=8, hp_max=8)

    apply_war_rig_hull_damage(hull, 6, armor=2, occupants=[occ], crash_save_outcomes=(True, False))
    await asyncio.sleep(0.05)

    assert occ.hp.current == 4, (
        f"one failed crash save costs exactly half max HP (8//2=4 → 8-4=4), got {occ.hp.current}"
    )


# ---------------------------------------------------------------------------
# MANDATORY OTEL wiring test (table half) — station verbs emit table.* spans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crewed_round_emits_table_spans_through_real_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the mandate: resolving a crewed war_rig round drives
    the ADR-129 table engine, so a ``component=='table'`` state_transition must
    surface through the real watcher route — proving the cooperative round is
    mechanically backed, not improvised prose. RED until the custom-beat seam
    emits a table span per station action."""
    import random

    import sidequest.game.table.war_rig  # noqa: F401
    from sidequest.game.table.engine import deal_table, resolve_table
    from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState

    captured = await _setup(monkeypatch, "war-rig-table-spans")

    seats = [
        TableSeat(
            seat_id=f"seat_{i}",
            party_name=f"Crew{i}",
            is_pc=True,
            status="active",
            private_state={},
        )
        for i in (1, 2, 3)
    ]
    st = TableState(
        game_kind="war_rig_crew",
        seats=seats,
        pot=TablePot(
            stake_kind="information",
            stake_descriptor="the convoy's fate",
            contributions={s.seat_id: 0 for s in seats},
        ),
        order=[s.seat_id for s in seats],
        dealer_seat="seat_1",
        max_decision_points=4,
    )
    deal_table(st, rng=random.Random(11))
    await asyncio.sleep(0.05)
    captured.clear()

    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="steer"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="shoot", target_seat="seat_3"),
        "seat_3": TableCommit(seat_id="seat_3", beat_id="repair"),
    }
    resolve_table(st, commits=commits, rng=random.Random(11))
    await asyncio.sleep(0.05)

    # Each of the 3 committed station verbs must emit its OWN table.commit span
    # through the real route — a truthy "at least one" check would pass even if
    # two of three verbs silently no-op'd (review 86-6). Pin one commit per verb
    # AND assert the distinct beat_ids so a dropped verb is caught.
    table_ops = _ops(captured, "table")
    commit_ops = [o for o in table_ops if o == "commit"]
    assert len(commit_ops) == 3, (
        "each of the 3 committed station verbs must drive one table.commit span; "
        f"got {len(commit_ops)} (all table ops: {table_ops}, "
        f"components: {[e.get('component') for e in captured]})"
    )
    committed_beats = {
        e["fields"].get("beat_id")
        for e in captured
        if e.get("component") == "table" and e["fields"].get("op") == "commit"
    }
    assert committed_beats == {"steer", "shoot", "repair"}, (
        f"each station verb must drive its OWN table.commit span (no silent no-op); "
        f"got {committed_beats}"
    )
