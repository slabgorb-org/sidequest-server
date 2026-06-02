"""Unit tests for Premise/Bloc content models (Plan 1, Task 1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.premises import (
    BlocAwakening,
    BlocDef,
    PremiseClaim,
    PremiseCollapse,
    PremiseDef,
    PremiseDrain,
    PremisesFile,
    WitnessedActArchetype,
    WitnessedActsFile,
)


def test_witnessed_act_archetype_is_vocabulary_only():
    act = WitnessedActArchetype(id="expose_the_humbug", label="Expose the Humbug")
    assert act.id == "expose_the_humbug"
    assert act.description == ""


def test_witnessed_acts_file_holds_a_list():
    f = WitnessedActsFile(
        witnessed_acts=[WitnessedActArchetype(id="refuse_the_premise", label="Refuse the Premise")]
    )
    assert len(f.witnessed_acts) == 1


def test_premise_def_round_trips():
    p = PremiseDef(
        premise_id="the_wizards_humbug",
        authority="the_wizard",
        claim=PremiseClaim(subject="the_wizard", proposition="The Wizard is great and terrible."),
        belief_reserve=90,
        propped_by=["munchkins"],
        drained_by=[PremiseDrain(act="expose_the_humbug", belief_delta=25, cost="public, risky")],
        collapse=PremiseCollapse(threshold=20, outcome="The Wizard flees in his balloon."),
    )
    assert p.belief_reserve == 90
    assert p.drained_by[0].belief_delta == 25
    assert p.collapse.vacuum_hook == ""


def test_bloc_def_round_trips():
    b = BlocDef(
        bloc_id="munchkins",
        grants_belief_to=["the_wizards_humbug"],
        awakening_acts=[BlocAwakening(act="show_defiance_survives", defiance_delta=15)],
        tipping_threshold=70,
        tipped_outcome="The Munchkins raise a revolt.",
    )
    assert b.defiance == 0  # starts low
    assert b.awakening_acts[0].defiance_delta == 15


def test_belief_reserve_is_bounded_0_to_100():
    with pytest.raises(ValidationError):
        PremiseDef(
            premise_id="x",
            authority="y",
            claim=PremiseClaim(subject="y", proposition="z"),
            belief_reserve=101,
            collapse=PremiseCollapse(threshold=0, outcome="o"),
        )


def test_drain_delta_must_be_positive():
    with pytest.raises(ValidationError):
        PremiseDrain(act="a", belief_delta=0)


def test_extra_keys_are_forbidden():
    with pytest.raises(ValidationError):
        PremiseDef.model_validate(
            {
                "premise_id": "x",
                "authority": "y",
                "claim": {"subject": "y", "proposition": "z"},
                "collapse": {"threshold": 0, "outcome": "o"},
                "typo_field": True,
            }
        )


def test_premises_file_defaults_to_empty_lists():
    f = PremisesFile()
    assert f.premises == []
    assert f.blocs == []
