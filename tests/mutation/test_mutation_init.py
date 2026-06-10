from __future__ import annotations

from sidequest.game.session import GameSnapshot
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.server.mutation_init import init_mutation_state_for_session


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[NegativeMutationDef(id="negative/frail", name="F",
                                       roll_range=(1, 100), effect="y")],
        positives=[PositiveMutationDef(id="structure/crushing_jaws", name="C",
                                       category="structure", effect="bite")],
    )


def _snapshot() -> GameSnapshot:
    # Mirror the minimal-GameSnapshot construction used by existing tests:
    # grep "GameSnapshot(" tests/ for the lightest fixture and reuse its shape.
    return GameSnapshot.model_construct(mutation_state=None)


def test_no_catalog_skips_silently() -> None:
    snap = _snapshot()
    init_mutation_state_for_session(
        snap, catalog=None, character_name="Rux", character_class="Mutant", session_id="s1",
    )
    assert snap.mutation_state is None


def test_mutant_seeds_snapshot_state() -> None:
    snap = _snapshot()
    init_mutation_state_for_session(
        snap, catalog=_catalog(), character_name="Rux", character_class="Mutant", session_id="s1",
    )
    assert snap.mutation_state is not None
    assert "Rux" in snap.mutation_state.characters


def test_non_mutant_leaves_no_character_entry() -> None:
    snap = _snapshot()
    init_mutation_state_for_session(
        snap, catalog=_catalog(), character_name="Rux", character_class="Scavenger", session_id="s1",
    )
    # container may exist (created on first init), but no entry for a non-mutant
    if snap.mutation_state is not None:
        assert "Rux" not in snap.mutation_state.characters
