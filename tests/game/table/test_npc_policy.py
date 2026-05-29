"""NPC policy unit tests.

All calls pass the full poker beat set as ``available_beats`` (the contract
introduced by the kind-general refactor).  Poker has all beats authored →
same choices as before.  Additional auction-kind-safety tests are below.
"""

import random

import pytest

import sidequest.game.table.auction  # noqa: F401  (registers auction kind)
import sidequest.game.table.poker  # noqa: F401  (registers poker kind)
from sidequest.game.table.engine import deal_table, resolve_table
from sidequest.game.table.npc_policy import decide_npc_commit
from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState

# Full poker beat set — all poker-authored beats.
_POKER_BEATS = {"fold", "call", "bet", "raise", "bluff", "read_table", "cheat", "accuse"}
# Auction beat set — as authored by the tea_and_murder confrontation definition.
_AUCTION_BEATS = {"raise_bid", "bluff", "read_room", "withdraw"}


def _state() -> TableState:
    seats = [
        TableSeat(seat_id="seat_1", party_name="PC", is_pc=True, status="active", private_state={}),
        TableSeat(
            seat_id="seat_2",
            party_name="Gambler",
            is_pc=False,
            status="active",
            private_state={
                "strength_band": "weak",
                "ocean": {"neuroticism": 0.9},
                "disposition": "neutral",
            },
        ),
    ]
    return TableState(
        game_kind="poker",
        seats=seats,
        pot=TablePot(
            stake_kind="money", stake_descriptor="pot", contributions={"seat_1": 5, "seat_2": 1}
        ),
        order=["seat_1", "seat_2"],
        dealer_seat="seat_1",
        max_decision_points=3,
    )


def _auction_state() -> TableState:
    seats = [
        TableSeat(
            seat_id="seat_1", party_name="PC", is_pc=True, status="active", private_state={}
        ),
        TableSeat(
            seat_id="seat_2",
            party_name="Bidder",
            is_pc=False,
            status="active",
            private_state={
                "strength_band": "weak",
                "ocean": {"neuroticism": 0.9},
                "disposition": "neutral",
                "valuation": 20,
                "max_bid": 20,
                "_current_bid": 0,
            },
        ),
    ]
    return TableState(
        game_kind="auction",
        seats=seats,
        pot=TablePot(
            stake_kind="item", stake_descriptor="the Ming vase", contributions={"seat_1": 0, "seat_2": 0}
        ),
        order=["seat_1", "seat_2"],
        dealer_seat="seat_1",
        max_decision_points=3,
    )


# ---------------------------------------------------------------------------
# Existing poker NPC tests — updated to pass the poker available_beats set.
# Outcomes are identical to before (poker has all beats authored).
# ---------------------------------------------------------------------------


def test_anxious_weak_npc_folds():
    st = _state()
    npc = st.find_seat("seat_2")
    commit = decide_npc_commit(st, npc, rng=random.Random(1), available_beats=_POKER_BEATS)
    assert commit.beat_id == "fold"


def test_confident_strong_npc_does_not_fold():
    st = _state()
    npc = st.find_seat("seat_2")
    npc.private_state["strength_band"] = "monster"
    npc.private_state["ocean"] = {"neuroticism": 0.1}
    commit = decide_npc_commit(st, npc, rng=random.Random(1), available_beats=_POKER_BEATS)
    assert commit.beat_id in ("raise", "call", "bluff")


def test_larcenous_disposition_can_cheat():
    st = _state()
    npc = st.find_seat("seat_2")
    npc.private_state["strength_band"] = "marginal"
    npc.private_state["ocean"] = {"neuroticism": 0.2}
    npc.private_state["disposition"] = "larcenous"
    # over many seeds, at least one cheat appears for a larcenous NPC
    # (poker has "cheat" authored → same behavior as before)
    beats = {
        decide_npc_commit(st, npc, rng=random.Random(s), available_beats=_POKER_BEATS).beat_id
        for s in range(40)
    }
    assert "cheat" in beats


def test_commit_seat_id_matches_npc():
    st = _state()
    npc = st.find_seat("seat_2")
    commit = decide_npc_commit(st, npc, rng=random.Random(0), available_beats=_POKER_BEATS)
    assert commit.seat_id == "seat_2"


def test_weak_calm_npc_folds_under_pot_pressure():
    st = _state()
    # large pot: weak + calm NPC should fold under commitment pressure
    st.pot.contributions["seat_1"] = 20
    npc = st.find_seat("seat_2")
    npc.private_state["strength_band"] = "weak"
    npc.private_state["ocean"] = {"neuroticism": 0.2}  # calm
    npc.private_state["disposition"] = "neutral"
    commit = decide_npc_commit(st, npc, rng=random.Random(3), available_beats=_POKER_BEATS)
    assert commit.beat_id == "fold"


def test_deterministic_under_seed():
    st = _state()
    npc = st.find_seat("seat_2")
    a = decide_npc_commit(st, npc, rng=random.Random(123), available_beats=_POKER_BEATS)
    b = decide_npc_commit(st, npc, rng=random.Random(123), available_beats=_POKER_BEATS)
    assert a == b


# ---------------------------------------------------------------------------
# Auction NPC kind-safety tests (C3 regression guard)
# ---------------------------------------------------------------------------


def test_auction_npc_never_returns_unauthored_beat():
    """Over many seeds and a LARCENOUS NPC, every returned beat ∈ auction set.

    This is the direct regression test for C3: a larcenous NPC at an auction
    must NEVER return "cheat" (not authored for auction → NotImplementedError
    at AuctionTableGame.cheat()) or any other non-auction beat.
    """
    st = _auction_state()
    npc = st.find_seat("seat_2")
    npc.private_state["strength_band"] = "marginal"
    npc.private_state["ocean"] = {"neuroticism": 0.2}
    npc.private_state["disposition"] = "larcenous"
    for seed in range(80):
        commit = decide_npc_commit(
            st, npc, rng=random.Random(seed), available_beats=_AUCTION_BEATS
        )
        assert commit.beat_id in _AUCTION_BEATS, (
            f"seed={seed}: got unauthored beat {commit.beat_id!r}; "
            f"auction beats={_AUCTION_BEATS}"
        )
    # "cheat" must never appear
    all_beats = {
        decide_npc_commit(
            st, npc, rng=random.Random(s), available_beats=_AUCTION_BEATS
        ).beat_id
        for s in range(80)
    }
    assert "cheat" not in all_beats, f"larcenous auction NPC emitted 'cheat': {all_beats}"


def test_weak_anxious_auction_npc_withdraws():
    """Weak + anxious auction NPC maps to the drop-out beat: "withdraw"."""
    st = _auction_state()
    npc = st.find_seat("seat_2")
    npc.private_state["strength_band"] = "weak"
    npc.private_state["ocean"] = {"neuroticism": 0.9}  # anxious
    commit = decide_npc_commit(st, npc, rng=random.Random(1), available_beats=_AUCTION_BEATS)
    assert commit.beat_id == "withdraw"


def test_strong_auction_npc_raises_bid():
    """Strong auction NPC maps to the aggressive beat: "raise_bid"."""
    st = _auction_state()
    npc = st.find_seat("seat_2")
    npc.private_state["strength_band"] = "monster"
    npc.private_state["ocean"] = {"neuroticism": 0.7}  # anxious enough to avoid slow-play
    commit = decide_npc_commit(st, npc, rng=random.Random(1), available_beats=_AUCTION_BEATS)
    assert commit.beat_id == "raise_bid"


def test_empty_available_beats_raises():
    """Empty available_beats is a content error — must fail loud."""
    st = _state()
    npc = st.find_seat("seat_2")
    with pytest.raises(ValueError, match="available_beats is empty"):
        decide_npc_commit(st, npc, rng=random.Random(0), available_beats=set())


# ---------------------------------------------------------------------------
# Auction NPC end-to-end no-crash test (C3 no-NotImplementedError)
# ---------------------------------------------------------------------------


def test_auction_larcenous_npc_resolve_no_notimplementederror():
    """Drive an auction resolve_table where a larcenous NPC auto-commits.

    The engine must NOT raise NotImplementedError from AuctionTableGame (which
    has no cheat() method).  The NPC policy, given only auction beats, must
    fall through the larcenous guard and return an authored bid/withdraw.
    """
    st = _auction_state()
    deal_table(st, rng=random.Random(7))
    npc = st.find_seat("seat_2")
    npc.private_state["disposition"] = "larcenous"
    npc.private_state["ocean"] = {"neuroticism": 0.2}

    # Simulate the narration_apply auto-commit path: decide then resolve.
    npc_commit = decide_npc_commit(
        st, npc, rng=random.Random(7), available_beats=_AUCTION_BEATS
    )
    assert npc_commit.beat_id in _AUCTION_BEATS

    pc_commit = TableCommit(seat_id="seat_1", beat_id="raise_bid", amount=5)
    commits = {"seat_1": pc_commit, "seat_2": npc_commit}

    # Must not raise NotImplementedError or any other exception.
    out = resolve_table(st, commits=commits, rng=random.Random(7))
    assert out is not None  # resolution returned a valid outcome
