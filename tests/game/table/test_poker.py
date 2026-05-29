import random

from sidequest.game.table.poker import POKER_BANDS, PokerTableGame, _band_for, _hand_strength
from sidequest.game.table.types import TablePot, TableSeat


def _seats(n: int) -> list[TableSeat]:
    return [
        TableSeat(
            seat_id=f"seat_{i}", party_name=f"P{i}", is_pc=True, status="active", private_state={}
        )
        for i in range(1, n + 1)
    ]


def test_deal_populates_real_hands_and_strength():
    game = PokerTableGame()
    seats = _seats(3)
    pot = TablePot(
        stake_kind="money", stake_descriptor="pot", contributions={s.seat_id: 0 for s in seats}
    )
    game.deal(seats, pot, random.Random(42))
    for s in seats:
        assert len(s.private_state["cards"]) == 5
        assert isinstance(s.private_state["strength"], int)
        assert s.private_state["strength_band"] in POKER_BANDS
        assert s.private_state["cheat_trace"] == 0.0
    # antes seeded the pot
    assert all(v > 0 for v in pot.contributions.values())


def test_deal_is_deterministic_under_seed():
    pot = TablePot(stake_kind="money", stake_descriptor="pot", contributions={"seat_1": 0})
    a = _seats(1)
    b = _seats(1)
    PokerTableGame().deal(a, pot, random.Random(7))
    pot2 = TablePot(stake_kind="money", stake_descriptor="pot", contributions={"seat_1": 0})
    PokerTableGame().deal(b, pot2, random.Random(7))
    assert a[0].private_state["cards"] == b[0].private_state["cards"]


def test_deal_no_duplicate_cards_across_seats():
    seats = _seats(4)
    pot = TablePot(
        stake_kind="money", stake_descriptor="pot", contributions={s.seat_id: 0 for s in seats}
    )
    PokerTableGame().deal(seats, pot, random.Random(99))
    all_cards = [c for s in seats for c in s.private_state["cards"]]
    assert len(all_cards) == len(set(all_cards)), "dealt the same card twice"


def test_strength_reads_private_state():
    game = PokerTableGame()
    seat = _seats(1)[0]
    seat.private_state["strength"] = 1234
    assert game.strength(seat) == 1234


def test_hand_strength_orders_pair_above_high_card():
    pair = ["AS", "AH", "5C", "9D", "2S"]
    high = ["AS", "KH", "5C", "9D", "2S"]
    assert _hand_strength(pair) > _hand_strength(high)


def test_band_correctness_high_card_is_weak():
    high = ["AS", "KH", "9C", "5D", "2S"]  # ace-high, no pair → category 0
    assert _band_for(_hand_strength(high)) == "weak"


def test_band_correctness_quads_is_monster():
    quads = ["9S", "9H", "9C", "9D", "2S"]  # four of a kind → category 7
    assert _band_for(_hand_strength(quads)) == "monster"


def test_cheat_never_degrades_a_made_hand():
    game = PokerTableGame()
    seat = _seats(1)[0]
    seat.private_state["cards"] = ["2H", "3H", "4H", "5H", "6H"]  # straight flush
    seat.private_state["strength"] = _hand_strength(seat.private_state["cards"])
    seat.private_state["cheat_trace"] = 0.0
    before = seat.private_state["strength"]
    result = game.cheat(seat, random.Random(1))
    assert result.strength_after >= before  # never sabotages
    assert seat.private_state["cheat_trace"] > 0.0  # attempt still leaves a trace
