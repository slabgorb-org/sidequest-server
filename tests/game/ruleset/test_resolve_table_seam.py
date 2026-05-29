import random

import sidequest.game.table.poker  # noqa: F401
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState


def _state(n: int, max_dp: int = 1) -> TableState:
    seats = [
        TableSeat(seat_id=f"seat_{i}", party_name=f"P{i}", is_pc=True, status="active", private_state={})
        for i in range(1, n + 1)
    ]
    return TableState(
        game_kind="poker", seats=seats,
        pot=TablePot(stake_kind="money", stake_descriptor="the pot",
                     contributions={s.seat_id: 0 for s in seats}),
        order=[s.seat_id for s in seats], dealer_seat="seat_1", max_decision_points=max_dp,
    )


def test_native_module_deals_and_resolves_through_seam():
    module = get_ruleset_module("native")
    st = _state(2, max_dp=1)
    module.deal_table(st, rng=random.Random(1))
    st.find_seat("seat_1").private_state["strength"] = 999
    st.find_seat("seat_2").private_state["strength"] = 1
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="call"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    out = module.resolve_table(st, commits=commits, rng=random.Random(1))
    assert out.showdown is True
    assert out.resolved_winner == "seat_1"


def test_seam_is_available_on_every_module():
    # Real smoke-call (not just hasattr): the inherited delegation must EXECUTE
    # on every module, not merely exist as a name.
    for slug in ("native", "swn", "cwn"):
        module = get_ruleset_module(slug)
        st = _state(2, max_dp=1)
        module.deal_table(st, rng=random.Random(42))  # inherited; must not raise
        assert all("strength" in s.private_state for s in st.seats)
