"""Unit tests for the Premise/Bloc cross-reference validator (Plan 1, Task 3)."""

from __future__ import annotations

import pytest

from sidequest.genre.error import GenreValidationError
from sidequest.genre.models.premises import (
    BlocAwakening,
    BlocDef,
    PremiseClaim,
    PremiseCollapse,
    PremiseDef,
    PremiseDrain,
)
from sidequest.genre.premise_validate import validate_premises


def _premise(**over):
    base = dict(
        premise_id="the_wizards_humbug",
        authority="the_wizard",
        claim=PremiseClaim(subject="the_wizard", proposition="The Wizard is great."),
        belief_reserve=90,
        propped_by=["munchkins"],
        drained_by=[PremiseDrain(act="expose_the_humbug", belief_delta=25)],
        collapse=PremiseCollapse(threshold=20, outcome="He flees."),
    )
    base.update(over)
    return PremiseDef(**base)


def _bloc(**over):
    base = dict(
        bloc_id="munchkins",
        grants_belief_to=["the_wizards_humbug"],
        awakening_acts=[BlocAwakening(act="show_defiance_survives", defiance_delta=15)],
        tipping_threshold=70,
        tipped_outcome="They revolt.",
    )
    base.update(over)
    return BlocDef(**base)


_NPCS = {"the_wizard"}
_ACTS = {"expose_the_humbug", "show_defiance_survives"}


def test_valid_set_passes():
    validate_premises(
        premises=[_premise()],
        blocs=[_bloc()],
        authored_npc_ids=_NPCS,
        valid_act_ids=_ACTS,
        world_slug="oz",
    )  # no raise


def test_unknown_authority_fails():
    with pytest.raises(GenreValidationError, match="authority"):
        validate_premises(
            premises=[_premise(authority="nobody")],
            blocs=[_bloc()],
            authored_npc_ids=_NPCS,
            valid_act_ids=_ACTS,
            world_slug="oz",
        )


def test_propped_by_unknown_bloc_fails():
    with pytest.raises(GenreValidationError, match="propped_by"):
        validate_premises(
            premises=[_premise(propped_by=["ghosts"])],
            blocs=[_bloc()],
            authored_npc_ids=_NPCS,
            valid_act_ids=_ACTS,
            world_slug="oz",
        )


def test_drain_act_outside_vocabulary_fails():
    with pytest.raises(GenreValidationError, match="vocabulary"):
        validate_premises(
            premises=[_premise(drained_by=[PremiseDrain(act="invent_act", belief_delta=5)])],
            blocs=[_bloc()],
            authored_npc_ids=_NPCS,
            valid_act_ids=_ACTS,
            world_slug="oz",
        )


def test_grants_belief_to_unknown_premise_fails():
    with pytest.raises(GenreValidationError, match="grants_belief_to"):
        validate_premises(
            premises=[_premise()],
            blocs=[_bloc(grants_belief_to=["no_such_premise"])],
            authored_npc_ids=_NPCS,
            valid_act_ids=_ACTS,
            world_slug="oz",
        )


def test_awakening_act_outside_vocabulary_fails():
    with pytest.raises(GenreValidationError, match="vocabulary"):
        validate_premises(
            premises=[_premise()],
            blocs=[_bloc(awakening_acts=[BlocAwakening(act="invent_act", defiance_delta=5)])],
            authored_npc_ids=_NPCS,
            valid_act_ids=_ACTS,
            world_slug="oz",
        )


def test_empty_content_is_valid():
    validate_premises(
        premises=[],
        blocs=[],
        authored_npc_ids=set(),
        valid_act_ids=set(),
        world_slug="oz",
    )  # no raise — a world with no politics is a valid authoring choice


def test_duplicate_premise_id_fails():
    with pytest.raises(GenreValidationError, match="duplicate"):
        validate_premises(
            premises=[_premise(), _premise()],
            blocs=[_bloc()],
            authored_npc_ids=_NPCS,
            valid_act_ids=_ACTS,
            world_slug="oz",
        )


def test_duplicate_bloc_id_fails():
    with pytest.raises(GenreValidationError, match="duplicate"):
        validate_premises(
            premises=[_premise()],
            blocs=[_bloc(), _bloc()],
            authored_npc_ids=_NPCS,
            valid_act_ids=_ACTS,
            world_slug="oz",
        )
