from __future__ import annotations

import pytest

from sidequest.mutation.acquire_ops import (
    acquire_positive,
    acquire_random_negative,
    roll_stigma,
)
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.state import CharacterMutationState, MutationState


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(
            body_part=["eyes", "skin", "hands", "spine", "jaw", "hair"],
            nature=["luminous", "scaled", "withered", "oversized", "translucent", "ridged"],
            flavor=[
                "amber",
                "silver",
                "weeping",
                "cracked",
                "humming",
                "cold",
                "hot",
                "twitching",
                "numb",
                "bright",
                "dark",
                "shifting",
            ],
        ),
        negatives=[
            NegativeMutationDef(
                id="negative/withered_arm",
                name="Withered Arm",
                roll_range=(1, 50),
                effect="weak arm",
            ),
            NegativeMutationDef(
                id="negative/frail", name="Frail", roll_range=(51, 100), effect="frail"
            ),
        ],
        positives=[
            PositiveMutationDef(
                id="structure/crushing_jaws",
                name="Crushing Jaws",
                category="structure",
                effect="bite",
            ),
            PositiveMutationDef(
                id="structure/savage_claws",
                name="Savage Claws",
                category="structure",
                effect="claws",
            ),
            PositiveMutationDef(
                id="sense/echo_location", name="Echo Location", category="sense", effect="sonar"
            ),
        ],
    )


def _state(mp: int = 0) -> MutationState:
    return MutationState(characters={"Rux": CharacterMutationState(mp_remaining=mp)})


def test_negative_grants_mp_and_logs() -> None:
    state, cat = _state(), _catalog()
    result = acquire_random_negative(state, cat, actor="Rux", session_id="s1", source="chargen")
    assert result.applied
    cs = state.characters["Rux"]
    assert cs.mp_remaining == cat.mp_economy.per_negative_mp
    assert cs.negative_ids == [result.mutation_id]
    assert cs.acquisition_log == [result.mutation_id]
    assert state.roll_sequence > 0


def test_negative_cap_refused() -> None:
    state, cat = _state(), _catalog()
    cs = state.characters["Rux"]
    cs.negative_ids = ["negative/a", "negative/b", "negative/c"]
    result = acquire_random_negative(state, cat, actor="Rux", session_id="s1", source="chargen")
    assert not result.applied
    assert "max_negatives" in result.reason


def test_negative_reroll_on_duplicate_is_deterministic() -> None:
    s1, s2 = _state(), _state()
    cat = _catalog()
    for state in (s1, s2):
        acquire_random_negative(state, cat, actor="Rux", session_id="s1", source="chargen")
        acquire_random_negative(state, cat, actor="Rux", session_id="s1", source="chargen")
    assert s1.characters["Rux"].negative_ids == s2.characters["Rux"].negative_ids
    assert len(set(s1.characters["Rux"].negative_ids)) == 2  # both table entries, no dupe
    assert s1.roll_sequence == s2.roll_sequence  # identical sequence consumption, rerolls included


def test_random_positive_costs_one() -> None:
    state, cat = _state(mp=2), _catalog()
    result = acquire_positive(state, cat, actor="Rux", session_id="s1", source="chargen")
    assert result.applied
    assert state.characters["Rux"].mp_remaining == 2 - cat.mp_economy.spend_random_positive
    assert result.mutation_id in {p.id for p in cat.positives}


def test_picked_positive_costs_three() -> None:
    state, cat = _state(mp=3), _catalog()
    result = acquire_positive(
        state,
        cat,
        actor="Rux",
        session_id="s1",
        source="chargen",
        mutation_id="sense/echo_location",
    )
    assert result.applied
    assert state.characters["Rux"].mp_remaining == 0
    assert state.characters["Rux"].positive_ids == ["sense/echo_location"]


def test_picked_already_owned_refused() -> None:
    state, cat = _state(mp=6), _catalog()
    state.characters["Rux"].positive_ids = ["sense/echo_location"]
    result = acquire_positive(
        state,
        cat,
        actor="Rux",
        session_id="s1",
        source="chargen",
        mutation_id="sense/echo_location",
    )
    assert not result.applied
    assert result.reason == "already_owned"
    assert state.characters["Rux"].mp_remaining == 6  # nothing spent


def test_second_same_category_costs_three_even_random() -> None:
    state, cat = _state(mp=4), _catalog()
    state.characters["Rux"].positive_ids = ["structure/crushing_jaws"]
    result = acquire_positive(
        state,
        cat,
        actor="Rux",
        session_id="s1",
        source="chargen",
        category="structure",
    )
    assert result.applied
    assert result.mutation_id == "structure/savage_claws"
    assert state.characters["Rux"].mp_remaining == 4 - cat.mp_economy.spend_same_category


def test_insufficient_mp_refused() -> None:
    state, cat = _state(mp=0), _catalog()
    result = acquire_positive(state, cat, actor="Rux", session_id="s1", source="chargen")
    assert not result.applied
    assert "insufficient_mp" in result.reason
    assert state.characters["Rux"].positive_ids == []


def test_unknown_actor_raises() -> None:
    state, cat = _state(), _catalog()
    with pytest.raises(KeyError, match="Nobody"):
        acquire_random_negative(state, cat, actor="Nobody", session_id="s1", source="chargen")


def test_stigma_rolls_three_tables() -> None:
    state, cat = _state(), _catalog()
    record = roll_stigma(state, cat, actor="Rux", session_id="s1")
    assert record is not None
    assert record.body_part in cat.stigma.body_part
    assert record.nature in cat.stigma.nature
    assert record.flavor in cat.stigma.flavor
    assert state.characters["Rux"].stigma == [record]


def test_concealable_stigma_costs_mp_and_refuses_when_broke() -> None:
    state, cat = _state(mp=0), _catalog()
    record = roll_stigma(state, cat, actor="Rux", session_id="s1", concealable=True)
    assert record is None  # refused — no MP for concealment
    state2 = _state(mp=1)
    record2 = roll_stigma(state2, cat, actor="Rux", session_id="s1", concealable=True)
    assert record2 is not None and record2.concealable
    assert state2.characters["Rux"].mp_remaining == 0
