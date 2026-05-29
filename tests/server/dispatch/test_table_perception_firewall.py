from sidequest.game.table.types import TablePot, TableSeat, TableState
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
