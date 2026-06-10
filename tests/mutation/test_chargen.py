from __future__ import annotations

import pytest

from sidequest.mutation.chargen import seed_character_mutations
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.state import MutationState


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"], chargen_negatives_rolled=1),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[
            NegativeMutationDef(
                id="negative/withered_arm", name="W", roll_range=(1, 50), effect="x"
            ),
            NegativeMutationDef(id="negative/frail", name="F", roll_range=(51, 100), effect="y"),
        ],
        positives=[
            PositiveMutationDef(
                id="structure/crushing_jaws", name="C", category="structure", effect="bite"
            ),
        ],
    )


def test_mutant_class_seeds_state() -> None:
    state = MutationState()
    cs = seed_character_mutations(
        state,
        _catalog(),
        actor="Rux",
        character_class="Mutant",
        session_id="s1",
    )
    assert cs is not None
    assert state.characters["Rux"] is cs
    # base_mp (2) + one rolled negative (+2)
    assert cs.mp_remaining == 4
    assert len(cs.negative_ids) == 1
    # negatives-first ordering: the log starts with the negative
    assert cs.acquisition_log[0].startswith("negative/")


def test_non_mutant_class_gets_none() -> None:
    state = MutationState()
    cs = seed_character_mutations(
        state,
        _catalog(),
        actor="Rux",
        character_class="Scavenger",
        session_id="s1",
    )
    assert cs is None
    assert "Rux" not in state.characters


def test_seeding_is_idempotent() -> None:
    state = MutationState()
    catalog = _catalog()
    first = seed_character_mutations(
        state,
        catalog,
        actor="Rux",
        character_class="Mutant",
        session_id="s1",
    )
    again = seed_character_mutations(
        state,
        catalog,
        actor="Rux",
        character_class="Mutant",
        session_id="s1",
    )
    assert again is first is not None
    assert len(state.characters["Rux"].negative_ids) == 1  # not re-rolled


def test_resume_safety_same_negative() -> None:
    catalogs = (_catalog(), _catalog())
    states = (MutationState(), MutationState())
    results = [
        seed_character_mutations(s, c, actor="Rux", character_class="Mutant", session_id="s1")
        for s, c in zip(states, catalogs, strict=True)
    ]
    assert results[0] is not None and results[1] is not None
    assert results[0].negative_ids == results[1].negative_ids


def test_failed_seeding_leaves_no_half_seeded_actor() -> None:
    catalog = MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"], chargen_negatives_rolled=2),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[
            NegativeMutationDef(id="negative/frail", name="F", roll_range=(1, 100), effect="y"),
        ],
        positives=[
            PositiveMutationDef(
                id="structure/crushing_jaws", name="C", category="structure", effect="bite"
            ),
        ],
    )
    state = MutationState()
    with pytest.raises(ValueError, match="non-duplicate"):
        seed_character_mutations(
            state,
            catalog,
            actor="Rux",
            character_class="Mutant",
            session_id="s1",
        )
    assert "Rux" not in state.characters  # no half-seeded residue
