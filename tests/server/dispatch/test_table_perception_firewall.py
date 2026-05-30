import random

import sidequest.game.table.poker  # noqa: F401  (registers poker)
from sidequest.game.table.engine import deal_table, resolve_table
from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState
from sidequest.server.dispatch.confrontation import project_table_frame_for_seat


def _state(resolved=False) -> TableState:
    seats = [
        TableSeat(
            seat_id="seat_1",
            party_name="A",
            is_pc=True,
            status="active",
            private_state={"cards": ["AS", "AH"], "strength_band": "strong"},
        ),
        TableSeat(
            seat_id="seat_2",
            party_name="B",
            is_pc=True,
            status="active",
            private_state={"cards": ["2C", "7D"], "strength_band": "weak"},
        ),
    ]
    st = TableState(
        game_kind="poker",
        seats=seats,
        pot=TablePot(
            stake_kind="money", stake_descriptor="pot", contributions={"seat_1": 2, "seat_2": 2}
        ),
        order=["seat_1", "seat_2"],
        dealer_seat="seat_1",
        max_decision_points=3,
        resolved_winner="seat_1" if resolved else None,
    )
    return st


def test_pre_showdown_frame_hides_other_seats_hand():
    st = _state(resolved=False)
    frame = project_table_frame_for_seat(st, seat_id="seat_1")
    own = next(s for s in frame["seats"] if s["seat_id"] == "seat_1")
    other = next(s for s in frame["seats"] if s["seat_id"] == "seat_2")
    assert own["private_state"]["cards"] == ["AS", "AH"]
    assert "cards" not in other.get("private_state", {})  # firewall
    # public table state always present
    assert frame["pot"]["contributions"] == {"seat_1": 2, "seat_2": 2}


def test_showdown_frame_reveals_all_hands():
    st = _state(resolved=True)
    frame = project_table_frame_for_seat(st, seat_id="seat_1")
    other = next(s for s in frame["seats"] if s["seat_id"] == "seat_2")
    assert other["private_state"]["cards"] == ["2C", "7D"]  # revealed


def test_unseated_socket_gets_public_only():
    st = _state(resolved=False)
    frame = project_table_frame_for_seat(st, seat_id=None)
    for s in frame["seats"]:
        assert "cards" not in s.get("private_state", {})


def test_resolved_but_unseated_seat_id_gets_public_only():
    # Spectator/observer case: a seated PC whose party_name matches NO TableSeat
    # resolves to a seat_id not in the table. Pre-showdown, an unknown seat_id
    # must project public-only — never another seat's cards.
    st = _state(resolved=False)
    frame = project_table_frame_for_seat(st, seat_id="seat_does_not_exist")
    for s in frame["seats"]:
        assert s.get("private_state", {}) == {}


def _engine_state(n: int = 2, max_dp: int = 3) -> TableState:
    """Minimal TableState for engine-level firewall tests (2+ seats, poker)."""
    seats = [
        TableSeat(
            seat_id=f"seat_{i}",
            party_name=f"P{i}",
            is_pc=True,
            status="active",
            private_state={"perception": 10, "concealment": 0},
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


def test_read_intel_lands_in_reader_private_frame():
    """After a read_table commit the reader's projected frame contains read_intel
    naming the target with the target's real strength_band.  The firewall delivers
    private_state to the reader's OWN seat projection and nowhere else."""
    st = _engine_state()
    deal_table(st, rng=random.Random(42))
    # Stamp target's strength_band so the read result is predictable
    st.find_seat("seat_2").private_state["strength_band"] = "monster"

    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="read_table", target_seat="seat_2"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    resolve_table(st, commits=commits, rng=random.Random(42))

    # The reader's own projected frame must contain read_intel
    reader_frame = project_table_frame_for_seat(st, seat_id="seat_1")
    reader_seat_proj = next(s for s in reader_frame["seats"] if s["seat_id"] == "seat_1")
    assert "read_intel" in reader_seat_proj["private_state"], (
        "read_intel missing from reader's own projected private_state"
    )
    intel_entries = reader_seat_proj["private_state"]["read_intel"]
    assert len(intel_entries) == 1
    assert intel_entries[0]["target_seat"] == "seat_2"
    assert intel_entries[0]["strength_band"] == "monster"


def test_read_intel_hidden_from_other_seat_pre_showdown():
    """Pre-showdown: the target's projected frame must NOT see the reader's read_intel."""
    st = _engine_state()
    deal_table(st, rng=random.Random(43))
    st.find_seat("seat_2").private_state["strength_band"] = "decent"

    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="read_table", target_seat="seat_2"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    resolve_table(st, commits=commits, rng=random.Random(43))

    # seat_2's projected frame: seat_1's private_state must be {} (firewall hides it)
    target_frame = project_table_frame_for_seat(st, seat_id="seat_2")
    reader_seat_as_seen_by_target = next(
        s for s in target_frame["seats"] if s["seat_id"] == "seat_1"
    )
    assert reader_seat_as_seen_by_target["private_state"] == {}, (
        "read_intel from seat_1 must not be visible to seat_2 pre-showdown"
    )


def test_read_intel_hidden_from_unseated_socket_pre_showdown():
    """An unseated/lobby socket must not see read_intel in any seat's private_state."""
    st = _engine_state()
    deal_table(st, rng=random.Random(44))
    st.find_seat("seat_2").private_state["strength_band"] = "weak"

    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="read_table", target_seat="seat_2"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
    }
    resolve_table(st, commits=commits, rng=random.Random(44))

    unseated_frame = project_table_frame_for_seat(st, seat_id=None)
    for seat_proj in unseated_frame["seats"]:
        assert seat_proj["private_state"] == {}, (
            f"seat {seat_proj['seat_id']} private_state should be empty for unseated socket"
        )


def test_multiple_reads_accumulate_in_reader_private_state():
    """Two read_table commits in successive decision points both land in
    read_intel as separate entries — a player remembers everything they've read."""
    st = _engine_state(n=3, max_dp=2)
    deal_table(st, rng=random.Random(55))
    st.find_seat("seat_2").private_state["strength_band"] = "strong"
    st.find_seat("seat_3").private_state["strength_band"] = "weak"

    # DP 0: seat_1 reads seat_2
    commits_dp0 = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="read_table", target_seat="seat_2"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
        "seat_3": TableCommit(seat_id="seat_3", beat_id="call"),
    }
    out0 = resolve_table(st, commits=commits_dp0, rng=random.Random(55))
    assert out0.showdown is False  # should be mid-hand

    # DP 1: seat_1 reads seat_3 → triggers showdown (max_dp=2, dp now=1 → ≥ max-1)
    commits_dp1 = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="read_table", target_seat="seat_3"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="call"),
        "seat_3": TableCommit(seat_id="seat_3", beat_id="call"),
    }
    resolve_table(st, commits=commits_dp1, rng=random.Random(55))

    reader_frame = project_table_frame_for_seat(st, seat_id="seat_1")
    reader_seat_proj = next(s for s in reader_frame["seats"] if s["seat_id"] == "seat_1")
    # showdown reveals all private_state so we can inspect; but even pre-showdown
    # this would be visible because seat_1 is asking for its own seat.
    intel = reader_seat_proj["private_state"]["read_intel"]
    targets = {entry["target_seat"] for entry in intel}
    assert "seat_2" in targets
    assert "seat_3" in targets
