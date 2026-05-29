import random

import sidequest.game.table.poker  # noqa: F401  (registers poker)
from sidequest.game.table.engine import deal_table, resolve_table
from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState


def _state(n: int, max_dp: int = 3) -> TableState:
    seats = [
        TableSeat(
            seat_id=f"seat_{i}",
            party_name=f"P{i}",
            is_pc=True,
            status="active",
            private_state={"perception": 12, "concealment": 8},
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


def test_cheat_raises_trace_and_changes_strength():
    st = _state(2)
    deal_table(st, rng=random.Random(1))
    seat = st.find_seat("seat_1")
    # force a weak hand so the cheat can only help
    seat.private_state["cards"] = ["2S", "3H", "4C", "7D", "9S"]
    seat.private_state["strength"] = 0
    seat.private_state["cheat_trace"] = 0.0
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="cheat"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    resolve_table(st, commits=commits, rng=random.Random(1))
    assert seat.private_state["cheat_trace"] > 0.0
    assert seat.private_state["strength"] > 0


def test_read_injects_real_info_into_outcome():
    st = _state(2)
    deal_table(st, rng=random.Random(2))
    st.find_seat("seat_2").private_state["strength_band"] = "monster"
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="read_table", target_seat="seat_2"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    out = resolve_table(st, commits=commits, rng=random.Random(2))
    assert "seat_1" in out.read_results
    assert out.read_results["seat_1"].info["strength_band"] == "monster"


def test_accuse_lands_forfeits_cheater_at_showdown():
    st = _state(2, max_dp=1)
    deal_table(st, rng=random.Random(3))
    cheater = st.find_seat("seat_2")
    cheater.private_state["strength"] = 10**9  # would win on raw strength
    cheater.private_state["cheat_trace"] = 1.0  # blatant
    accuser = st.find_seat("seat_1")
    accuser.private_state["strength"] = 1
    accuser.private_state["perception"] = 20  # near-certain catch
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="accuse", target_seat="seat_2"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    out = resolve_table(st, commits=commits, rng=random.Random(3))
    assert out.showdown is True
    assert "seat_2" in out.forfeited_seats
    assert out.resolved_winner == "seat_1"  # forfeit beats raw strength


def test_accuse_whiff_penalizes_accuser():
    st = _state(2, max_dp=1)
    deal_table(st, rng=random.Random(4))
    honest = st.find_seat("seat_2")
    honest.private_state["strength"] = 5
    honest.private_state["cheat_trace"] = 0.0  # nothing to find
    honest.private_state["concealment"] = 30
    accuser = st.find_seat("seat_1")
    accuser.private_state["strength"] = 10**9  # would win on strength
    accuser.private_state["perception"] = 1
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="accuse", target_seat="seat_2"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    out = resolve_table(st, commits=commits, rng=random.Random(4))
    # whiff → accuser forfeits (slandered an honest man), honest seat wins
    assert "seat_1" in out.forfeited_seats
    assert out.resolved_winner == "seat_2"


def test_accusation_in_earlier_dp_resolved_at_showdown_against_final_trace():
    st = _state(2, max_dp=2)
    deal_table(st, rng=random.Random(11))
    accuser = st.find_seat("seat_1")
    cheater = st.find_seat("seat_2")
    accuser.private_state["perception"] = 20
    cheater.private_state["concealment"] = 0
    cheater.private_state["cheat_trace"] = 0.0
    cheater.private_state["strength"] = 10**9  # would win on raw strength
    accuser.private_state["strength"] = 1
    # DP0: seat_1 accuses; nobody folds → NOT showdown yet (decision_point advances)
    out0 = resolve_table(
        st,
        commits={
            "seat_1": TableCommit(seat_id="seat_1", beat_id="accuse", target_seat="seat_2"),
            "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
        },
        rng=random.Random(11),
    )
    assert out0.showdown is False
    assert st.pending_accusations == [("seat_1", "seat_2")]  # persisted, not discarded
    # DP1: the cheat happens NOW (after the accusation) → showdown rolls vs the raised trace
    out1 = resolve_table(
        st,
        commits={
            "seat_1": TableCommit(seat_id="seat_1", beat_id="call"),
            "seat_2": TableCommit(seat_id="seat_2", beat_id="cheat"),
        },
        rng=random.Random(11),
    )
    assert out1.showdown is True
    assert "seat_2" in out1.forfeited_seats  # late cheat IS caught
    assert out1.resolved_winner == "seat_1"  # forfeit beats raw strength
