"""Runtime PoliticalState models + hydration (Plan 2, Task 1)."""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.game.political_state import (
    BeliefLedgerEntry,
    BlocState,
    PoliticalState,
    PremiseState,
)


def _world(premises, blocs):
    # Duck-typed stand-in for a genre World (only .premises/.blocs are read).
    return SimpleNamespace(premises=premises, blocs=blocs)


def _premise_def(premise_id="p1", belief_reserve=90):
    return SimpleNamespace(premise_id=premise_id, belief_reserve=belief_reserve)


def _bloc_def(bloc_id="b1", defiance=5):
    return SimpleNamespace(bloc_id=bloc_id, defiance=defiance)


def test_from_world_seeds_live_dials_from_content():
    state = PoliticalState.from_world(
        _world([_premise_def("the_wizards_humbug", 90)], [_bloc_def("munchkins", 5)])
    )
    assert state is not None
    assert state.premises["the_wizards_humbug"].belief_reserve == 90
    assert state.premises["the_wizards_humbug"].collapsed is False
    assert state.blocs["munchkins"].defiance == 5
    assert state.blocs["munchkins"].tipped is False
    assert state.ledger == []


def test_from_world_returns_none_when_no_politics():
    # A world with no premises/blocs is a valid authoring choice — NOT an error,
    # and NOT an empty container. None keeps the precondition gate honest.
    assert PoliticalState.from_world(_world([], [])) is None


def test_ledger_entry_round_trips():
    e = BeliefLedgerEntry(
        turn=3,
        act_id="expose_the_humbug",
        target_id="the_wizards_humbug",
        target_kind="premise",
        effect="drained",
        delta=-35,
        new_value=55,
        witnesses=["Dorothy", "Toto"],
    )
    assert e.delta == -35
    assert e.witnesses == ["Dorothy", "Toto"]


def test_political_state_serializes_round_trip():
    state = PoliticalState(
        premises={"p1": PremiseState(premise_id="p1", belief_reserve=40, collapsed=False)},
        blocs={"b1": BlocState(bloc_id="b1", defiance=20, tipped=False)},
        ledger=[],
    )
    dumped = state.model_dump_json()
    restored = PoliticalState.model_validate_json(dumped)
    assert restored.premises["p1"].belief_reserve == 40
    assert restored.blocs["b1"].defiance == 20


def test_snapshot_carries_optional_political_state():
    # Wiring: GameSnapshot must hold the runtime state and default to None
    # (a save from before this feature, or a world with no politics).
    from sidequest.game.session import GameSnapshot

    snap = GameSnapshot()
    assert snap.political_state is None
    snap.political_state = PoliticalState(
        premises={"p1": PremiseState(premise_id="p1", belief_reserve=10)},
        blocs={},
        ledger=[],
    )
    restored = GameSnapshot.model_validate_json(snap.model_dump_json())
    assert restored.political_state is not None
    assert restored.political_state.premises["p1"].belief_reserve == 10
