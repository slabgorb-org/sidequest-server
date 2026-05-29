import random

import pytest

# poker kind must be imported so it registers
import sidequest.game.table.poker  # noqa: F401
from sidequest.game.table.engine import deal_table, resolve_table
from sidequest.game.table.types import (
    TableCommit,
    TableNeedsOthersError,
    TablePot,
    TableSeat,
    TableState,
)


def _state(n: int, max_dp: int = 3) -> TableState:
    seats = [
        TableSeat(
            seat_id=f"seat_{i}", party_name=f"P{i}", is_pc=True, status="active", private_state={}
        )
        for i in range(1, n + 1)
    ]
    return TableState(
        game_kind="poker",
        seats=seats,
        pot=TablePot(
            stake_kind="money",
            stake_descriptor="the pot",
            contributions={s.seat_id: 0 for s in seats},
        ),
        order=[s.seat_id for s in seats],
        dealer_seat="seat_1",
        max_decision_points=max_dp,
    )


def test_deal_table_requires_two_seats():
    st = _state(1)
    with pytest.raises(TableNeedsOthersError):
        deal_table(st, rng=random.Random(0))


def test_deal_table_unknown_kind_fails_loud():
    from sidequest.game.table.registry import UnknownTableGameError

    st = _state(2)
    st.game_kind = "nonesuch"
    with pytest.raises(UnknownTableGameError):
        deal_table(st, rng=random.Random(0))


def test_fold_drops_seat_and_emits():
    st = _state(3)
    deal_table(st, rng=random.Random(1))
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="fold"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
        "seat_3": TableCommit(seat_id="seat_3", beat_id="call"),
    }
    out = resolve_table(st, commits=commits, rng=random.Random(1))
    assert st.find_seat("seat_1").status == "folded"
    assert not out.showdown  # 2 active seats remain, decision_point advanced
    assert st.decision_point == 1


def test_bet_raise_adjusts_pot():
    st = _state(2)
    deal_table(st, rng=random.Random(2))
    before = st.pot.contributions["seat_1"]
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="raise", amount=5),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call", amount=5),
    }
    resolve_table(st, commits=commits, rng=random.Random(2))
    assert st.pot.contributions["seat_1"] == before + 5


def test_showdown_when_one_active_seat_left():
    st = _state(3)
    deal_table(st, rng=random.Random(3))
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="call"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="fold"),
        "seat_3": TableCommit(seat_id="seat_3", beat_id="fold"),
    }
    out = resolve_table(st, commits=commits, rng=random.Random(3))
    assert out.showdown is True
    assert out.resolved_winner == "seat_1"
    assert out.pot_awarded_to == "seat_1"
    assert st.resolved_winner == "seat_1"


def test_showdown_at_max_decision_points_picks_highest_strength():
    st = _state(2, max_dp=1)
    deal_table(st, rng=random.Random(4))
    # force known strengths so the winner is deterministic
    st.find_seat("seat_1").private_state["strength"] = 999999
    st.find_seat("seat_2").private_state["strength"] = 1
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="call"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    out = resolve_table(st, commits=commits, rng=random.Random(4))
    assert out.showdown is True
    assert out.resolved_winner == "seat_1"


def test_showdown_requires_readable_strength():
    st = _state(2, max_dp=1)
    deal_table(st, rng=random.Random(5))
    del st.find_seat("seat_2").private_state["strength"]
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="call"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    with pytest.raises(ValueError, match="strength"):
        resolve_table(st, commits=commits, rng=random.Random(5))


def test_commit_for_unknown_seat_fails_loud():
    st = _state(2)
    deal_table(st, rng=random.Random(6))
    commits = {"ghost": TableCommit(seat_id="ghost", beat_id="call")}
    with pytest.raises(ValueError, match="ghost"):
        resolve_table(st, commits=commits, rng=random.Random(6))
