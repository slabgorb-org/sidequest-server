from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.game.table.types import TablePot, TableSeat, TableState


def _enc(**kw) -> StructuredEncounter:
    base = dict(
        encounter_type="poker",
        player_metric=EncounterMetric(name="player", threshold=10),
        opponent_metric=EncounterMetric(name="opponent", threshold=10),
    )
    base.update(kw)
    return StructuredEncounter(**base)


def test_table_state_defaults_none():
    enc = _enc()
    assert enc.table_state is None


def test_table_state_round_trips():
    seats = [
        TableSeat(seat_id="seat_1", party_name="P1", is_pc=True, status="active", private_state={}),
        TableSeat(seat_id="seat_2", party_name="P2", is_pc=False, status="active", private_state={}),
    ]
    ts = TableState(
        game_kind="poker",
        seats=seats,
        pot=TablePot(stake_kind="money", stake_descriptor="pot", contributions={"seat_1": 0, "seat_2": 0}),
        order=["seat_1", "seat_2"],
        dealer_seat="seat_1",
        max_decision_points=3,
    )
    enc = _enc(win_condition="table_showdown", table_state=ts)
    rebuilt = StructuredEncounter.model_validate(enc.model_dump())
    assert rebuilt.table_state is not None
    assert rebuilt.table_state.game_kind == "poker"
    assert rebuilt.win_condition == "table_showdown"
