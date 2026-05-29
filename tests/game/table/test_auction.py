import random

import sidequest.game.table.auction  # noqa: F401  (registers auction)
from sidequest.game.table.auction import AuctionTableGame
from sidequest.game.table.engine import deal_table, resolve_table
from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState


def _state(n=2, max_dp=1) -> TableState:
    seats = [
        TableSeat(seat_id=f"seat_{i}", party_name=f"P{i}", is_pc=True, status="active", private_state={})
        for i in range(1, n + 1)
    ]
    return TableState(
        game_kind="auction", seats=seats,
        pot=TablePot(stake_kind="item", stake_descriptor="the Ming vase",
                     contributions={s.seat_id: 0 for s in seats}),
        order=[s.seat_id for s in seats], dealer_seat="seat_1", max_decision_points=max_dp,
    )


def test_deal_assigns_secret_valuations():
    game = AuctionTableGame()
    seats = _state(3).seats
    pot = TablePot(stake_kind="item", stake_descriptor="vase", contributions={s.seat_id: 0 for s in seats})
    game.deal(seats, pot, random.Random(1))
    for s in seats:
        assert s.private_state["valuation"] > 0
        assert s.private_state["max_bid"] == s.private_state["valuation"]
        assert "strength_band" in s.private_state  # NPC policy reads this


def test_highest_standing_bid_wins_at_showdown():
    st = _state(2, max_dp=1)
    deal_table(st, rng=random.Random(2))
    # strength() for auction = current bid contribution
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="raise_bid", amount=10),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="raise_bid", amount=3),
    }
    out = resolve_table(st, commits=commits, rng=random.Random(2))
    assert out.showdown is True
    assert out.resolved_winner == "seat_1"


def test_over_max_bid_loses_at_showdown():
    st = _state(2, max_dp=1)
    deal_table(st, rng=random.Random(2))
    # seat_1 bids far over its max_bid → strength -1 → loses despite the higher chip count
    over = st.find_seat("seat_1").private_state["max_bid"] + 100
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="raise_bid", amount=over),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="raise_bid", amount=2),
    }
    out = resolve_table(st, commits=commits, rng=random.Random(2))
    assert out.resolved_winner == "seat_2"  # seat_1 overbid its budget → strength -1


def test_withdraw_folds_seat():
    st = _state(3, max_dp=3)
    deal_table(st, rng=random.Random(3))
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="raise_bid", amount=5),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="withdraw"),
        "seat_3": TableCommit(seat_id="seat_3", beat_id="raise_bid", amount=4),
    }
    resolve_table(st, commits=commits, rng=random.Random(3))
    assert st.find_seat("seat_2").status == "folded"


def test_read_room_returns_rival_valuation_band():
    st = _state(2, max_dp=3)
    deal_table(st, rng=random.Random(4))
    st.find_seat("seat_2").private_state["strength_band"] = "decent"
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="read_room", target_seat="seat_2"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="raise_bid", amount=1),
    }
    out = resolve_table(st, commits=commits, rng=random.Random(4))
    assert out.read_results["seat_1"].info["strength_band"] == "decent"
