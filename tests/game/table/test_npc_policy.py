import random

from sidequest.game.table.npc_policy import decide_npc_commit
from sidequest.game.table.types import TablePot, TableSeat, TableState


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


def test_anxious_weak_npc_folds():
    st = _state()
    npc = st.find_seat("seat_2")
    commit = decide_npc_commit(st, npc, rng=random.Random(1))
    assert commit.beat_id == "fold"


def test_confident_strong_npc_does_not_fold():
    st = _state()
    npc = st.find_seat("seat_2")
    npc.private_state["strength_band"] = "monster"
    npc.private_state["ocean"] = {"neuroticism": 0.1}
    commit = decide_npc_commit(st, npc, rng=random.Random(1))
    assert commit.beat_id in ("raise", "call", "bluff")


def test_larcenous_disposition_can_cheat():
    st = _state()
    npc = st.find_seat("seat_2")
    npc.private_state["strength_band"] = "marginal"
    npc.private_state["ocean"] = {"neuroticism": 0.2}
    npc.private_state["disposition"] = "larcenous"
    # over many seeds, at least one cheat appears for a larcenous NPC
    beats = {decide_npc_commit(st, npc, rng=random.Random(s)).beat_id for s in range(40)}
    assert "cheat" in beats


def test_commit_seat_id_matches_npc():
    st = _state()
    npc = st.find_seat("seat_2")
    commit = decide_npc_commit(st, npc, rng=random.Random(0))
    assert commit.seat_id == "seat_2"


def test_weak_calm_npc_folds_under_pot_pressure():
    st = _state()
    # large pot: weak + calm NPC should fold under commitment pressure
    st.pot.contributions["seat_1"] = 20
    npc = st.find_seat("seat_2")
    npc.private_state["strength_band"] = "weak"
    npc.private_state["ocean"] = {"neuroticism": 0.2}  # calm
    npc.private_state["disposition"] = "neutral"
    commit = decide_npc_commit(st, npc, rng=random.Random(3))
    assert commit.beat_id == "fold"


def test_deterministic_under_seed():
    st = _state()
    npc = st.find_seat("seat_2")
    a = decide_npc_commit(st, npc, rng=random.Random(123))
    b = decide_npc_commit(st, npc, rng=random.Random(123))
    assert a == b
