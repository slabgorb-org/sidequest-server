"""Story 86-7 (RED): Command Points economy + Crisis table — unit contract.

The crewed War Rig (86-6) shipped the table-engine half: stations dealt, station
verbs (steer/shoot/repair/scan) resolved through the custom-beat seam, a
vessel-scoped shared Hull that fans crash saves to every occupant. 86-7 layers
the SWN §4.3 *command* crunch on top:

  • **Command Points** — a per-vessel SHARED resource (spec §5 G5). Three actions:
      - ``do_your_duty``        (0 CP — standard resolution)
      - ``above_and_beyond``    (1 CP — +1d6 to the roll/pool)
      - ``support_department``  (1 CP — assist another station)
    CP persists across rounds and depletes as actions are taken. Spending more CP
    than the pool holds FAILS LOUD (No Silent Fallbacks) — a crew can't conjure
    command out of nothing.

  • **Crisis table** — a d10 table (spec §4.3.4). Each face is a ``continuing``
    (rolls each round until resolved, escalating) or ``acute`` (one-round) crisis.
    The ``deal_with_crisis`` action rolls d10 + ability vs the crisis DC; a failed
    *continuing* crisis ESCALATES and feeds its hull penalty into 86-2's two-pool
    rig damage model (reuse, not reimplement). An *acute* crisis does not continue.

**Proposed seam (TEA contract, open to Dev refinement) — mirrors 86-6's
``war_rig_combat`` module shape (pydantic pool + pure resolver + OTEL):**

    sidequest.game.war_rig_command:
      CommandPointPool(current, max, vessel_id)          # vessel-scoped, fail-loud
      CP_DO_YOUR_DUTY / CP_ABOVE_AND_BEYOND / CP_SUPPORT_DEPARTMENT  # action names
      CP_ACTION_COSTS: dict[str, int]                    # {duty:0, a&b:1, support:1}
      spend_command_points(pool, action, *, seat, rng) -> CommandPointSpendResult
        — unknown action -> ValueError; cost > current -> ValueError;
          above_and_beyond grants a 1..6 bonus; mutates pool in place.

      CrisisType = "continuing" | "acute"
      CrisisEntry(crisis_id, crisis_type, dc, description, hull_penalty)
      CRISIS_TABLE: dict[int, CrisisEntry]               # faces 1..10, minimal playable
      roll_crisis(rng, *, vessel_id) -> CrisisRollResult # d10 lookup
      deal_with_crisis(entry, *, seat, ability_mod, rng, vessel_id, hull=None)
          -> CrisisResolutionResult
        — total = d10 + ability_mod; success iff total >= dc; a failed CONTINUING
          crisis escalates (applies hull_penalty to the optional shared Hull); a
          failed ACUTE crisis does not escalate.

This file pins the BEHAVIOUR + fail-loud contract; OTEL is pinned in
``tests/integration/test_war_rig_command_otel.py``; content in
``tests/genre/test_war_rig_command_content.py``. Imports live inside the test
bodies so a missing module fails THESE tests, not collection of the suite.

RED until Dev builds ``sidequest.game.war_rig_command``.
"""

from __future__ import annotations

import random

import pytest

# ---------------------------------------------------------------------------
# Command Points — pool construction + fail-loud bounds
# ---------------------------------------------------------------------------


def test_command_point_pool_blank_vessel_id_fails_loud():
    """The CP pool is vessel-scoped; a blank vessel_id must fail loud rather than
    smuggle anonymous command into the OTEL attribution (mirrors WarRigHull)."""
    from sidequest.game.war_rig_command import CommandPointPool

    with pytest.raises(ValueError):
        CommandPointPool(current=3, max=3, vessel_id="   ")


def test_command_point_pool_current_cannot_exceed_max():
    """A pool can't start over-full — guards a content typo that hands the crew
    more command than the vessel can hold."""
    from sidequest.game.war_rig_command import CommandPointPool

    with pytest.raises(ValueError):
        CommandPointPool(current=5, max=3, vessel_id="war_rig_alpha")


def test_command_point_pool_current_cannot_be_negative():
    from sidequest.game.war_rig_command import CommandPointPool

    with pytest.raises(ValueError):
        CommandPointPool(current=-1, max=3, vessel_id="war_rig_alpha")


# ---------------------------------------------------------------------------
# Command Points — the three actions and their costs
# ---------------------------------------------------------------------------


def test_cp_action_costs_match_swn():
    """SWN §4.3: Do Your Duty is free, Above and Beyond and Support Department
    each cost 1 CP. Pin the exact cost map so a silent re-pricing is caught."""
    from sidequest.game.war_rig_command import (
        CP_ABOVE_AND_BEYOND,
        CP_ACTION_COSTS,
        CP_DO_YOUR_DUTY,
        CP_SUPPORT_DEPARTMENT,
    )

    assert CP_ACTION_COSTS[CP_DO_YOUR_DUTY] == 0
    assert CP_ACTION_COSTS[CP_ABOVE_AND_BEYOND] == 1
    assert CP_ACTION_COSTS[CP_SUPPORT_DEPARTMENT] == 1


def test_do_your_duty_costs_no_command_points():
    """The free action must NOT deplete the pool — otherwise a standard station
    action silently bleeds command every round."""
    from sidequest.game.war_rig_command import (
        CP_DO_YOUR_DUTY,
        CommandPointPool,
        spend_command_points,
    )

    pool = CommandPointPool(current=2, max=2, vessel_id="war_rig_alpha")
    result = spend_command_points(pool, CP_DO_YOUR_DUTY, seat="seat_1", rng=random.Random(1))

    assert result.cost == 0, f"do_your_duty must cost 0 CP, got {result.cost}"
    assert pool.current == 2, f"do_your_duty must not deplete the pool, got {pool.current}"


def test_above_and_beyond_depletes_one_command_point():
    from sidequest.game.war_rig_command import (
        CP_ABOVE_AND_BEYOND,
        CommandPointPool,
        spend_command_points,
    )

    pool = CommandPointPool(current=2, max=2, vessel_id="war_rig_alpha")
    result = spend_command_points(pool, CP_ABOVE_AND_BEYOND, seat="seat_1", rng=random.Random(1))

    assert result.cost == 1, f"above_and_beyond must cost 1 CP, got {result.cost}"
    assert pool.current == 1, f"above_and_beyond must deplete the pool by 1, got {pool.current}"
    assert result.old_current == 2 and result.new_current == 1, (
        f"spend result must report the realized delta (2→1), got "
        f"{result.old_current}→{result.new_current}"
    )


def test_above_and_beyond_grants_a_d6_bonus():
    """Above and Beyond buys a +1d6 — the bonus must land in [1, 6], the d6
    range, not 0 (which would mean the crew paid 1 CP for nothing)."""
    from sidequest.game.war_rig_command import (
        CP_ABOVE_AND_BEYOND,
        CommandPointPool,
        spend_command_points,
    )

    # Fresh 1-CP pool per sample — we're sampling the d6 across seeds, not
    # draining one shared pool (over-spending would correctly hit the fail-loud
    # insufficient-CP guard, which is a different test).
    bonuses = {
        spend_command_points(
            CommandPointPool(current=1, max=1, vessel_id="war_rig_alpha"),
            CP_ABOVE_AND_BEYOND,
            seat="seat_1",
            rng=random.Random(seed),
        ).bonus
        for seed in range(20)
    }
    assert bonuses, "above_and_beyond must report a bonus"
    assert all(1 <= b <= 6 for b in bonuses), (
        f"above_and_beyond bonus must be a d6 in [1,6], saw {sorted(bonuses)}"
    )


def test_support_department_assists_for_one_command_point():
    from sidequest.game.war_rig_command import (
        CP_SUPPORT_DEPARTMENT,
        CommandPointPool,
        spend_command_points,
    )

    pool = CommandPointPool(current=2, max=2, vessel_id="war_rig_alpha")
    result = spend_command_points(pool, CP_SUPPORT_DEPARTMENT, seat="seat_2", rng=random.Random(1))

    assert result.cost == 1, f"support_department must cost 1 CP, got {result.cost}"
    assert pool.current == 1
    assert result.bonus == 0, (
        f"support_department grants no d6 bonus (that is above_and_beyond's alone), got {result.bonus}"
    )


def test_command_point_pool_max_zero_fails_loud():
    """A 0-max pool is degenerate — the validator must reject it (mirrors WarRigHull)."""
    from sidequest.game.war_rig_command import CommandPointPool

    with pytest.raises(ValueError):
        CommandPointPool(current=0, max=0, vessel_id="war_rig_alpha")


# ---------------------------------------------------------------------------
# Command Points — fail-loud + persistence (No Silent Fallbacks)
# ---------------------------------------------------------------------------


def test_spending_more_than_the_pool_holds_fails_loud():
    """A crew with 0 CP cannot pull Above and Beyond out of nowhere. The spend
    must raise, NOT silently floor at 0 or no-op — silent failure would let the
    narrator claim a bonus the engine never granted (the lie-detector mandate)."""
    from sidequest.game.war_rig_command import (
        CP_ABOVE_AND_BEYOND,
        CommandPointPool,
        spend_command_points,
    )

    pool = CommandPointPool(current=0, max=3, vessel_id="war_rig_alpha")
    with pytest.raises(ValueError):
        spend_command_points(pool, CP_ABOVE_AND_BEYOND, seat="seat_1", rng=random.Random(1))
    assert pool.current == 0, "a rejected spend must leave the pool untouched"


def test_unknown_cp_action_fails_loud():
    """No Silent Fallbacks: an unrecognized CP action must raise, not no-op."""
    from sidequest.game.war_rig_command import CommandPointPool, spend_command_points

    pool = CommandPointPool(current=3, max=3, vessel_id="war_rig_alpha")
    with pytest.raises(ValueError):
        spend_command_points(pool, "requisition_airstrike", seat="seat_1", rng=random.Random(1))


def test_command_points_persist_and_deplete_across_rounds():
    """AC1: the CP pool persists across rounds and depletes cumulatively — two
    Above-and-Beyonds drain a 2-CP pool to empty, and the third fails loud."""
    from sidequest.game.war_rig_command import (
        CP_ABOVE_AND_BEYOND,
        CommandPointPool,
        spend_command_points,
    )

    pool = CommandPointPool(current=2, max=2, vessel_id="war_rig_alpha")
    spend_command_points(pool, CP_ABOVE_AND_BEYOND, seat="seat_1", rng=random.Random(1))
    spend_command_points(pool, CP_ABOVE_AND_BEYOND, seat="seat_2", rng=random.Random(2))
    assert pool.current == 0, f"two A&B spends must drain a 2-CP pool, got {pool.current}"
    with pytest.raises(ValueError):
        spend_command_points(pool, CP_ABOVE_AND_BEYOND, seat="seat_3", rng=random.Random(3))


# ---------------------------------------------------------------------------
# Crisis table — shape (d10, continuing/acute)
# ---------------------------------------------------------------------------


def test_crisis_table_covers_every_d10_face():
    """A d10 Crisis table must have an entry for each face 1..10 — a hole would
    make ``roll_crisis`` KeyError on a live roll (fail-loud is fine, but a
    PLAYABLE table per spec §6 covers the whole die)."""
    from sidequest.game.war_rig_command import CRISIS_TABLE

    assert set(CRISIS_TABLE.keys()) == set(range(1, 11)), (
        f"Crisis table must cover faces 1..10, got {sorted(CRISIS_TABLE.keys())}"
    )


def test_crisis_table_has_both_continuing_and_acute_entries():
    """Both crisis kinds must be reachable from the table — otherwise one of the
    two resolution branches (escalation vs one-round) is dead in play."""
    from sidequest.game.war_rig_command import CRISIS_TABLE

    types = {e.crisis_type for e in CRISIS_TABLE.values()}
    assert types == {"continuing", "acute"}, (
        f"Crisis table must contain both 'continuing' and 'acute' entries, got {sorted(types)}"
    )


def test_roll_crisis_returns_the_rolled_faces_entry():
    """roll_crisis maps the d10 to its table entry; the rolled face is in range
    and the entry is the table's entry for that face."""
    from sidequest.game.war_rig_command import CRISIS_TABLE, roll_crisis

    result = roll_crisis(random.Random(4), vessel_id="war_rig_alpha")
    assert 1 <= result.roll <= 10, f"crisis roll must be a d10, got {result.roll}"
    assert result.entry is CRISIS_TABLE[result.roll], (
        "roll_crisis must return the table entry for the rolled face"
    )


# ---------------------------------------------------------------------------
# Crisis table — Deal With a Crisis resolution
# ---------------------------------------------------------------------------


def test_deal_with_crisis_succeeds_when_total_meets_dc():
    """d10 + ability vs DC: a trivially-easy crisis (dc=0) is always resolved."""
    from sidequest.game.war_rig_command import CrisisEntry, deal_with_crisis

    entry = CrisisEntry(
        crisis_id="loose_cargo",
        crisis_type="acute",
        dc=0,
        description="A strap snaps; the load shifts.",
        hull_penalty=1,
    )
    result = deal_with_crisis(
        entry, seat="seat_1", ability_mod=0, rng=random.Random(1), vessel_id="war_rig_alpha"
    )
    assert result.success is True, "dc=0 crisis must always be resolved"
    assert result.escalated is False, "a resolved crisis must not escalate"


def test_failed_continuing_crisis_escalates_and_damages_the_hull():
    """A failed CONTINUING crisis escalates and feeds its hull penalty into the
    86-2 two-pool model (reuse). dc=99 forces failure deterministically."""
    from sidequest.game.war_rig_combat import WarRigHull
    from sidequest.game.war_rig_command import CrisisEntry, deal_with_crisis

    hull = WarRigHull(current=6, max=6, base_max=6, vessel_id="war_rig_alpha")
    entry = CrisisEntry(
        crisis_id="engine_fire",
        crisis_type="continuing",
        dc=99,  # impossible — guarantees the failure branch
        description="Flames lick from under the hood and spread.",
        hull_penalty=2,
    )
    result = deal_with_crisis(
        entry,
        seat="seat_3",
        ability_mod=0,
        rng=random.Random(1),
        vessel_id="war_rig_alpha",
        hull=hull,
    )
    assert result.success is False, "dc=99 crisis cannot be resolved"
    assert result.escalated is True, "a failed continuing crisis must escalate"
    assert hull.current == 4, (
        f"escalation must apply the hull_penalty (6-2=4) through the shared Hull, got {hull.current}"
    )


def test_failed_acute_crisis_does_not_escalate():
    """An ACUTE crisis is a one-round threat: failing it does NOT escalate and
    does NOT keep damaging the Hull — distinguishing it from the continuing path."""
    from sidequest.game.war_rig_combat import WarRigHull
    from sidequest.game.war_rig_command import CrisisEntry, deal_with_crisis

    hull = WarRigHull(current=6, max=6, base_max=6, vessel_id="war_rig_alpha")
    entry = CrisisEntry(
        crisis_id="blinding_dust",
        crisis_type="acute",
        dc=99,
        description="A dust devil swallows the road for a heartbeat.",
        hull_penalty=2,
    )
    result = deal_with_crisis(
        entry,
        seat="seat_4",
        ability_mod=0,
        rng=random.Random(1),
        vessel_id="war_rig_alpha",
        hull=hull,
    )
    assert result.success is False
    assert result.escalated is False, "an acute crisis must not escalate"
    assert hull.current == 6, (
        f"a failed acute crisis must not damage the Hull (one-round threat), got {hull.current}"
    )


def test_deal_with_crisis_boundary_total_equal_dc_succeeds():
    """The success comparison is `total >= dc` — exercise the exact boundary so a
    `>=`→`>` off-by-one is caught. With a known roll R and ability_mod m, a crisis
    of DC R+m gives total == dc, which must SUCCEED."""
    from sidequest.game.war_rig_command import CrisisEntry, deal_with_crisis

    seed, ability_mod = 3, 2
    roll = random.Random(seed).randint(1, 10)  # the roll deal_with_crisis will produce
    entry = CrisisEntry(
        crisis_id="boundary",
        crisis_type="continuing",
        dc=roll + ability_mod,  # total == dc exactly
        description="On the knife's edge.",
        hull_penalty=1,
    )
    result = deal_with_crisis(
        entry,
        seat="seat_1",
        ability_mod=ability_mod,
        rng=random.Random(seed),
        vessel_id="war_rig_alpha",
    )
    assert result.total == entry.dc, (
        f"setup sanity: total {result.total} should equal dc {entry.dc}"
    )
    assert result.success is True, "total == dc must resolve the crisis (>= boundary)"
    assert result.escalated is False


def test_deal_with_crisis_boundary_one_below_dc_fails():
    """The companion boundary: total == dc-1 must FAIL (and a continuing crisis
    then escalates)."""
    from sidequest.game.war_rig_command import CrisisEntry, deal_with_crisis

    seed, ability_mod = 3, 2
    roll = random.Random(seed).randint(1, 10)
    entry = CrisisEntry(
        crisis_id="boundary",
        crisis_type="continuing",
        dc=roll + ability_mod + 1,  # total == dc - 1
        description="Just short.",
        hull_penalty=1,
    )
    result = deal_with_crisis(
        entry,
        seat="seat_1",
        ability_mod=ability_mod,
        rng=random.Random(seed),
        vessel_id="war_rig_alpha",
    )
    assert result.total == entry.dc - 1, (
        f"setup sanity: total {result.total} should be dc-1 {entry.dc - 1}"
    )
    assert result.success is False, "total == dc-1 must fail"
    assert result.escalated is True, "a failed continuing crisis escalates"


def test_deal_with_crisis_applies_negative_ability_mod_to_the_total():
    """A negative ability_mod (poor stat on a stat-checked crisis) must subtract
    from the roll — pin the arithmetic, not just the boolean branch."""
    from sidequest.game.war_rig_command import CrisisEntry, deal_with_crisis

    entry = CrisisEntry(
        crisis_id="arith",
        crisis_type="acute",
        dc=5,
        description="Test of arithmetic.",
        hull_penalty=1,
    )
    result = deal_with_crisis(
        entry, seat="seat_1", ability_mod=-3, rng=random.Random(7), vessel_id="war_rig_alpha"
    )
    assert result.total == result.roll - 3, (
        f"total must be roll + ability_mod (roll {result.roll} + (-3)), got {result.total}"
    )
