from __future__ import annotations

from sidequest.mutation.rolls import deterministic_roll


def test_same_inputs_same_roll() -> None:
    a = deterministic_roll(session_id="s1", actor="Rux", purpose="negative_d100", sequence=1, sides=100)
    b = deterministic_roll(session_id="s1", actor="Rux", purpose="negative_d100", sequence=1, sides=100)
    assert a == b


def test_sequence_changes_roll_distribution() -> None:
    rolls = {
        deterministic_roll(session_id="s1", actor="Rux", purpose="negative_d100", sequence=i, sides=100)
        for i in range(50)
    }
    assert len(rolls) > 10  # not constant; 50 draws over d100 must vary


def test_in_range() -> None:
    for i in range(200):
        r = deterministic_roll(session_id="s1", actor="Rux", purpose="stigma_flavor", sequence=i, sides=12)
        assert 1 <= r <= 12


def test_actor_and_purpose_independent() -> None:
    a = deterministic_roll(session_id="s1", actor="Rux", purpose="p1", sequence=1, sides=100)
    b = deterministic_roll(session_id="s1", actor="Kel", purpose="p1", sequence=1, sides=100)
    c = deterministic_roll(session_id="s1", actor="Rux", purpose="p2", sequence=1, sides=100)
    assert not (a == b == c)  # at least one input dimension moves the result
