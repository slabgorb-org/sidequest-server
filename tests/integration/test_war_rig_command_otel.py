"""Story 86-7 (RED): Command Points + Crisis OTEL — the GM panel is the lie detector.

AC3: every CP action emits ``command_points.*`` spans; every crisis event emits
``crisis.*`` spans; spans carry ``vessel_id`` + ``seat`` so the GM panel can render
the command economy and active crises. Per CLAUDE.md's OTEL Observability
Principle, a subsystem that doesn't emit is indistinguishable from the narrator
improvising — so these spans are load-bearing, not cosmetic.

The harness is the real ``TracerProvider`` + ``WatcherSpanProcessor`` +
``watcher_hub`` route, identical to ``tests/integration/test_war_rig_crew_combat.py``
(86-6) and ``test_rig_two_pool_combat.py`` (86-2). We assert the spans surface as
ROUTED ``state_transition`` events (component ``command_points`` / ``crisis``), the
same shape the GM dashboard's Subsystems tab renders for ``rig_pool.*`` — so Dev
must add SPAN_ROUTES entries, not just fire raw spans into the firehose.

**TEA contract (open to Dev refinement on the exact op strings):**
  - command_points.action_taken -> component="command_points", op="action_taken"
  - command_points.delta        -> component="command_points", op="delta"  (cost>0 only)
  - crisis.rolled               -> component="crisis", op="rolled"
  - crisis.resolved             -> component="crisis", op="resolved"
  - crisis.escalated            -> component="crisis", op="escalated"

The final test is the MANDATORY production-path wiring test (AC3): a real
``war_rig_crew`` round through ``resolve_table`` fires BOTH span families.

RED until Dev builds + routes the spans and wires CP/crisis into the kind.
Imports live inside the test bodies so a missing module fails THESE tests.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


async def _setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    """Bind a fresh watcher subscriber + local tracer; return the captured stream.

    Same harness as test_war_rig_crew_combat.py so the assertions exercise the
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


def _events(captured: list[dict], component: str) -> list[dict]:
    return [
        e
        for e in captured
        if e.get("event_type") == "state_transition" and e.get("component") == component
    ]


def _ops(captured: list[dict], component: str) -> list[str]:
    return [e["fields"].get("op") for e in _events(captured, component) if e["fields"].get("op")]


# ---------------------------------------------------------------------------
# Command Points spans through the real watcher route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_above_and_beyond_emits_action_taken_and_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1-CP action publishes BOTH command_points.action_taken and
    command_points.delta (the pool change) through the real route, carrying the
    vessel and seat the GM panel attributes the spend to."""
    from sidequest.game.war_rig_command import (
        CP_ABOVE_AND_BEYOND,
        CommandPointPool,
        spend_command_points,
    )

    captured = await _setup(monkeypatch, "cp-above-and-beyond")
    pool = CommandPointPool(current=2, max=2, vessel_id="war_rig_alpha")
    await asyncio.sleep(0.05)
    captured.clear()

    spend_command_points(pool, CP_ABOVE_AND_BEYOND, seat="seat_1", rng=random.Random(1))
    await asyncio.sleep(0.05)

    ops = _ops(captured, "command_points")
    assert "action_taken" in ops, f"command_points.action_taken must fire (got {ops})"
    assert "delta" in ops, f"command_points.delta must fire on a 1-CP spend (got {ops})"

    deltas = [e for e in _events(captured, "command_points") if e["fields"].get("op") == "delta"]
    f = deltas[0]["fields"]
    assert f.get("delta") == -1, f"the spend delta must be -1 CP, got {f.get('delta')}"
    assert f.get("vessel_id") == "war_rig_alpha", (
        f"CP spans must carry vessel_id for GM-panel attribution, got {f.get('vessel_id')!r}"
    )
    assert f.get("seat") == "seat_1", (
        f"CP spans must carry the spending seat, got {f.get('seat')!r}"
    )


@pytest.mark.asyncio
async def test_do_your_duty_emits_action_taken_but_no_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative case: the FREE action records that it happened (action_taken) but
    must NOT publish a pool delta — a spurious 0-CP delta would mislead the GM
    panel into showing command drain that never occurred."""
    from sidequest.game.war_rig_command import (
        CP_DO_YOUR_DUTY,
        CommandPointPool,
        spend_command_points,
    )

    captured = await _setup(monkeypatch, "cp-do-your-duty")
    pool = CommandPointPool(current=2, max=2, vessel_id="war_rig_alpha")
    await asyncio.sleep(0.05)
    captured.clear()

    spend_command_points(pool, CP_DO_YOUR_DUTY, seat="seat_1", rng=random.Random(1))
    await asyncio.sleep(0.05)

    ops = _ops(captured, "command_points")
    assert "action_taken" in ops, f"the free action must still record action_taken (got {ops})"
    assert "delta" not in ops, f"a 0-CP action must NOT publish a pool delta (got {ops})"


# ---------------------------------------------------------------------------
# Crisis spans through the real watcher route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roll_crisis_emits_crisis_rolled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rolling a crisis publishes crisis.rolled through the real route with the
    vessel and the rolled face, so the GM panel can show the active threat."""
    from sidequest.game.war_rig_command import roll_crisis

    captured = await _setup(monkeypatch, "crisis-rolled")
    await asyncio.sleep(0.05)
    captured.clear()

    result = roll_crisis(random.Random(2), vessel_id="war_rig_alpha")
    await asyncio.sleep(0.05)

    rolled = [e for e in _events(captured, "crisis") if e["fields"].get("op") == "rolled"]
    assert len(rolled) == 1, f"exactly one crisis.rolled expected (got {len(rolled)})"
    f = rolled[0]["fields"]
    assert f.get("vessel_id") == "war_rig_alpha", (
        f"crisis spans must carry vessel_id, got {f.get('vessel_id')!r}"
    )
    assert f.get("roll") == result.roll, (
        f"crisis.rolled must report the rolled face {result.roll}, got {f.get('roll')}"
    )


@pytest.mark.asyncio
async def test_failed_continuing_crisis_emits_resolved_and_escalated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed CONTINUING crisis publishes crisis.resolved (success=false) AND
    crisis.escalated, plus the reused rig_pool.delta from the hull penalty — the
    GM panel sees the crisis worsen and the Hull take the hit in one chain."""
    from sidequest.game.war_rig_combat import WarRigHull
    from sidequest.game.war_rig_command import CrisisEntry, deal_with_crisis

    captured = await _setup(monkeypatch, "crisis-escalate")
    hull = WarRigHull(current=6, max=6, base_max=6, vessel_id="war_rig_alpha")
    await asyncio.sleep(0.05)
    captured.clear()

    entry = CrisisEntry(
        crisis_id="engine_fire",
        crisis_type="continuing",
        dc=99,
        description="Flames spread under the hood.",
        hull_penalty=2,
    )
    deal_with_crisis(
        entry,
        seat="seat_3",
        ability_mod=0,
        rng=random.Random(1),
        vessel_id="war_rig_alpha",
        hull=hull,
    )
    await asyncio.sleep(0.05)

    crisis_ops = _ops(captured, "crisis")
    assert "resolved" in crisis_ops, f"crisis.resolved must fire (got {crisis_ops})"
    assert "escalated" in crisis_ops, (
        f"a failed continuing crisis must fire crisis.escalated (got {crisis_ops})"
    )
    resolved = [e for e in _events(captured, "crisis") if e["fields"].get("op") == "resolved"]
    assert resolved[0]["fields"].get("success") is False, (
        "crisis.resolved must record success=false for the failed deal"
    )
    # Reuse proof: the escalation must drive the 86-2 rig_pool.delta, not a
    # bespoke damage path — the Hull penalty surfaces on the rig component.
    rig_deltas = [e for e in _events(captured, "rig") if e["fields"].get("op") == "delta"]
    assert any(e["fields"].get("delta") == -2 for e in rig_deltas), (
        f"escalation must apply the -2 hull penalty via rig_pool.delta (reuse), "
        f"saw rig deltas {[e['fields'].get('delta') for e in rig_deltas]}"
    )


@pytest.mark.asyncio
async def test_resolved_crisis_does_not_escalate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative case: a SUCCESSFULLY dealt crisis fires crisis.resolved but must
    NOT fire crisis.escalated — escalation is the failure branch only."""
    from sidequest.game.war_rig_command import CrisisEntry, deal_with_crisis

    captured = await _setup(monkeypatch, "crisis-resolved-clean")
    await asyncio.sleep(0.05)
    captured.clear()

    entry = CrisisEntry(
        crisis_id="loose_cargo",
        crisis_type="continuing",
        dc=0,  # trivially resolved
        description="A strap snaps.",
        hull_penalty=2,
    )
    deal_with_crisis(
        entry, seat="seat_1", ability_mod=0, rng=random.Random(1), vessel_id="war_rig_alpha"
    )
    await asyncio.sleep(0.05)

    crisis_ops = _ops(captured, "crisis")
    assert "resolved" in crisis_ops, f"crisis.resolved must fire on success (got {crisis_ops})"
    assert "escalated" not in crisis_ops, f"a resolved crisis must NOT escalate (got {crisis_ops})"


# ---------------------------------------------------------------------------
# MANDATORY production-path wiring test — a crewed round fires BOTH span families
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crewed_round_fires_both_command_and_crisis_span_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 wiring mandate: a live ``war_rig_crew`` round driven through the
    production ``resolve_table`` path — a seat taking a CP action and a seat
    dealing with a crisis — must surface BOTH the command_points.* and crisis.*
    span families through the real watcher route.

    This is the test that proves the kind is actually WIRED (CLAUDE.md "Verify
    Wiring, Not Just Existence"): the CP/crisis functions existing and emitting
    in isolation is not enough — the ``war_rig_crew`` custom-beat seam must invoke
    them so a real round lights up the GM panel.

    TEA contract (open to Dev refinement): ``deal`` seeds a starting
    CommandPointPool into the table's shared state sufficient for the round, and
    ``above_and_beyond`` / ``deal_with_crisis`` are recognized station verbs that
    resolve through ``custom_beat``. If Dev models the shared CP store or the
    crisis trigger differently, THIS helper's commit-building is the seam that
    adapts — but both span families must surface from the production round.
    """
    import sidequest.game.table.war_rig  # noqa: F401  (registers war_rig_crew)
    from sidequest.game.table.engine import deal_table, resolve_table
    from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState

    captured = await _setup(monkeypatch, "war-rig-command-wiring")

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
        "seat_1": TableCommit(seat_id="seat_1", beat_id="above_and_beyond"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="deal_with_crisis"),
        "seat_3": TableCommit(seat_id="seat_3", beat_id="steer"),
    }
    resolve_table(st, commits=commits, rng=random.Random(11))
    await asyncio.sleep(0.05)

    # Assert the specific OPS fired, not just that the component appeared — a
    # bare no-op span carrying the right component name would pass a membership
    # check (reviewer 86-7). The CP action must record action_taken; the crisis
    # verb must roll AND resolve.
    cp_ops = _ops(captured, "command_points")
    assert "action_taken" in cp_ops, (
        "the above_and_beyond commit must drive command_points.action_taken through the "
        f"real route; saw command_points ops {cp_ops}"
    )
    crisis_ops = _ops(captured, "crisis")
    assert "rolled" in crisis_ops, (
        f"the deal_with_crisis commit must roll a crisis (crisis.rolled); saw {crisis_ops}"
    )
    assert "resolved" in crisis_ops, (
        f"the deal_with_crisis commit must resolve the crisis (crisis.resolved); saw {crisis_ops}"
    )


@pytest.mark.asyncio
async def test_in_round_continuing_crisis_escalation_damages_the_shared_hull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's HIGH fix: a failed continuing crisis in a LIVE war_rig_crew
    round must actually damage the crew's shared Hull (AC2), not just fire a span.

    Drives the production ``custom_beat`` path with a deterministic rng (seed 0 →
    crisis face 7 'engine_fire', continuing, DC 12; resolve roll 7 → fails →
    escalates, hull_penalty 2). Asserts the shared Hull on ``shared_state`` drops
    AND the escalation drives a real ``rig_pool.delta`` through the watcher route —
    proving the crisis→two-pool wiring is live in a round, not just at the
    function level."""
    import random as _random

    import sidequest.game.table.war_rig  # noqa: F401  (registers war_rig_crew)
    from sidequest.game.table.registry import get_table_game
    from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState

    captured = await _setup(monkeypatch, "war-rig-inround-escalation")
    game = get_table_game("war_rig_crew")
    seat = TableSeat(
        seat_id="seat_1", party_name="Boss", is_pc=True, status="active", private_state={}
    )
    st = TableState(
        game_kind="war_rig_crew",
        seats=[seat],
        pot=TablePot(stake_kind="information", stake_descriptor="survival", contributions={}),
        order=["seat_1"],
        dealer_seat="seat_1",
        max_decision_points=4,
    )
    await asyncio.sleep(0.05)
    captured.clear()

    commit = TableCommit(seat_id="seat_1", beat_id="deal_with_crisis")
    game.custom_beat(st, seat, commit, rng=_random.Random(0))
    await asyncio.sleep(0.05)

    hull = st.shared_state["war_rig_hull"]
    assert hull.current == 4, (
        f"a failed continuing crisis in-round must damage the shared Hull (6-2=4), got {hull.current}"
    )
    assert "escalated" in _ops(captured, "crisis"), (
        "crisis.escalated must fire on the failed continuing crisis"
    )
    rig_deltas = [e for e in _events(captured, "rig") if e["fields"].get("op") == "delta"]
    assert any(e["fields"].get("delta") == -2 for e in rig_deltas), (
        "the in-round escalation must drive a real rig_pool.delta of -2 (the two-pool model), "
        f"saw rig deltas {[e['fields'].get('delta') for e in rig_deltas]}"
    )


@pytest.mark.asyncio
async def test_support_department_emits_action_taken_and_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """support_department is also a 1-CP action — it must publish BOTH
    command_points.action_taken and command_points.delta through the real route,
    exactly like above_and_beyond. Guards against the delta emission being gated
    on action identity rather than cost (which would dark-panel this action)."""
    from sidequest.game.war_rig_command import (
        CP_SUPPORT_DEPARTMENT,
        CommandPointPool,
        spend_command_points,
    )

    captured = await _setup(monkeypatch, "cp-support-department")
    pool = CommandPointPool(current=2, max=2, vessel_id="war_rig_alpha")
    await asyncio.sleep(0.05)
    captured.clear()

    spend_command_points(pool, CP_SUPPORT_DEPARTMENT, seat="seat_2", rng=random.Random(1))
    await asyncio.sleep(0.05)

    ops = _ops(captured, "command_points")
    assert "action_taken" in ops, f"support_department must fire action_taken (got {ops})"
    assert "delta" in ops, f"support_department (1 CP) must fire delta (got {ops})"
    deltas = [e for e in _events(captured, "command_points") if e["fields"].get("op") == "delta"]
    assert deltas[0]["fields"].get("delta") == -1, "support_department delta must be -1 CP"


@pytest.mark.asyncio
async def test_command_points_persist_across_two_resolve_table_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 production-path persistence: the SHARED CP pool lives on shared_state
    and depletes cumulatively across SEPARATE resolve_table rounds — not re-seeded
    each round. Two rounds, each spending one CP, must leave the 4-CP pool at 2."""
    import sidequest.game.table.war_rig  # noqa: F401
    from sidequest.game.table.engine import deal_table, resolve_table
    from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState

    await _setup(monkeypatch, "cp-cross-round-persist")
    seats = [
        TableSeat(
            seat_id=f"seat_{i}", party_name=f"C{i}", is_pc=True, status="active", private_state={}
        )
        for i in (1, 2)
    ]
    st = TableState(
        game_kind="war_rig_crew",
        seats=seats,
        pot=TablePot(
            stake_kind="information",
            stake_descriptor="the convoy",
            contributions={s.seat_id: 0 for s in seats},
        ),
        order=[s.seat_id for s in seats],
        dealer_seat="seat_1",
        max_decision_points=6,
    )
    deal_table(st, rng=random.Random(11))

    # Round 1: seat_1 spends a CP; seat_2 steers (no CP).
    resolve_table(
        st,
        commits={
            "seat_1": TableCommit(seat_id="seat_1", beat_id="above_and_beyond"),
            "seat_2": TableCommit(seat_id="seat_2", beat_id="steer"),
        },
        rng=random.Random(11),
    )
    assert st.shared_state["command_points"].current == 3, "round 1 must drain 4→3"

    # Round 2: same shared pool must persist and drain again, not re-seed to 4.
    resolve_table(
        st,
        commits={
            "seat_1": TableCommit(seat_id="seat_1", beat_id="above_and_beyond"),
            "seat_2": TableCommit(seat_id="seat_2", beat_id="steer"),
        },
        rng=random.Random(12),
    )
    assert st.shared_state["command_points"].current == 2, (
        "the shared CP pool must persist across rounds and drain cumulatively (4→3→2), "
        f"got {st.shared_state['command_points'].current}"
    )
