import random

import pytest

from sidequest.game.table import registry as _reg
from sidequest.game.table.registry import (
    TableGame,
    UnknownTableGameError,
    get_table_game,
    register_table_game,
)
from sidequest.game.table.types import TablePot, TableSeat


@pytest.fixture(autouse=True)
def _restore_registry():
    before = dict(_reg._REGISTRY)
    yield
    _reg._REGISTRY.clear()
    _reg._REGISTRY.update(before)


def test_unknown_kind_fails_loud():
    with pytest.raises(UnknownTableGameError, match="frobnicate"):
        get_table_game("frobnicate")


def test_register_and_get_roundtrip():
    class _Dummy(TableGame):
        kind = "dummy_test_kind"

        def deal(self, seats, pot, rng):
            for s in seats:
                s.private_state["strength"] = 1
                s.private_state["strength_band"] = "weak"

        def strength(self, seat):
            return int(seat.private_state["strength"])

    register_table_game(_Dummy())
    game = get_table_game("dummy_test_kind")
    seat = TableSeat(
        seat_id="seat_1", party_name="P1", is_pc=True, status="active", private_state={}
    )
    pot = TablePot(stake_kind="money", stake_descriptor="pot", contributions={"seat_1": 0})
    game.deal([seat], pot, random.Random(1))
    assert game.strength(seat) == 1


def test_default_cheat_and_read_raise_not_implemented():
    class _NoExtras(TableGame):
        kind = "no_extras_test_kind"

        def deal(self, seats, pot, rng):
            pass

        def strength(self, seat):
            return 0

    g = _NoExtras()
    seat = TableSeat(
        seat_id="seat_1", party_name="P1", is_pc=True, status="active", private_state={}
    )
    with pytest.raises(NotImplementedError):
        g.cheat(seat, random.Random(0))
    with pytest.raises(NotImplementedError):
        g.read(seat, seat, reader_stat=0)


def test_double_register_same_kind_raises():
    class _A(TableGame):
        kind = "dup_kind_test"

        def deal(self, seats, pot, rng):
            pass

        def strength(self, seat):
            return 0

    register_table_game(_A())
    with pytest.raises(ValueError, match="already registered"):
        register_table_game(_A())
