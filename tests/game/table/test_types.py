import pytest
from pydantic import ValidationError

from sidequest.game.table.types import (
    TableNeedsOthersError,
    TablePot,
    TableResolutionOutcome,
    TableSeat,
    TableState,
)


def _seat(seat_id: str, party: str, *, is_pc: bool = True) -> TableSeat:
    return TableSeat(
        seat_id=seat_id,
        party_name=party,
        is_pc=is_pc,
        status="active",
        private_state={},
    )


def _state(n: int = 2) -> TableState:
    seats = [_seat(f"seat_{i}", f"P{i}") for i in range(1, n + 1)]
    return TableState(
        game_kind="poker",
        seats=seats,
        pot=TablePot(
            stake_kind="money",
            stake_descriptor="the pot",
            contributions={s.seat_id: 0 for s in seats},
        ),
        order=[s.seat_id for s in seats],
        dealer_seat=seats[0].seat_id,
        max_decision_points=3,
    )


def test_table_state_round_trips_and_defaults():
    st = _state(3)
    assert st.decision_point == 0
    assert st.resolved_winner is None
    # round-trip through pydantic serialization
    rebuilt = TableState.model_validate(st.model_dump())
    assert rebuilt == st


def test_extra_field_forbidden_on_seat():
    with pytest.raises(ValidationError):
        TableSeat(
            seat_id="seat_1",
            party_name="P1",
            is_pc=True,
            status="active",
            private_state={},
            bogus="x",
        )


def test_seat_status_literal_rejects_unknown():
    with pytest.raises(ValidationError):
        TableSeat(
            seat_id="seat_1",
            party_name="P1",
            is_pc=True,
            status="dancing",
            private_state={},
        )


def test_active_seat_ids_helper():
    st = _state(3)
    # engine mutates seat status in place during play — this mutability is
    # intentional (don't add frozen=True to TableSeat).
    st.seats[1].status = "folded"
    assert st.active_seat_ids() == ["seat_1", "seat_3"]


def test_find_seat_helper():
    st = _state(2)
    assert st.find_seat("seat_2").party_name == "P2"
    assert st.find_seat("nope") is None


def test_table_needs_others_error_is_value_error():
    assert issubclass(TableNeedsOthersError, ValueError)


def test_resolution_outcome_holds_award():
    out = TableResolutionOutcome(
        showdown=True,
        resolved_winner="seat_1",
        pot_awarded_to="seat_1",
        stake_kind="money",
        stake_descriptor="the pot",
        narration_hint="seat_1 rakes the pot",
        forfeited_seats=["seat_2"],
    )
    assert out.showdown is True
    assert out.pot_awarded_to == "seat_1"
