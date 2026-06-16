"""Tests for the AuthoredNpc pydantic model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.authored_npc import AuthoredNpc


def test_minimal_authored_npc_parses() -> None:
    npc = AuthoredNpc(id="kestrel_engineer", name="Sora")
    assert npc.id == "kestrel_engineer"
    assert npc.name == "Sora"
    assert npc.role == ""
    assert npc.initial_disposition == 0
    assert npc.history_seeds == []


def test_full_authored_npc_parses() -> None:
    npc = AuthoredNpc(
        id="kestrel_captain",
        name="Mira-not-invented",
        pronouns="she/her",
        role="captain",
        ocean={"O": 0.5, "C": 0.7, "E": 0.4, "A": 0.5, "N": 0.4},
        appearance="Tall, weathered, salt-grey braid.",
        age="late 40s",
        distinguishing_features=["augmetic forearm"],
        history_seeds=["flew Hegemony patrol before going freelance"],
        initial_disposition=60,
    )
    assert npc.role == "captain"
    assert npc.initial_disposition == 60
    assert npc.ocean == {"O": 0.5, "C": 0.7, "E": 0.4, "A": 0.5, "N": 0.4}


def test_initial_disposition_below_min_rejected() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to -100"):
        AuthoredNpc(id="x", name="X", initial_disposition=-101)


def test_initial_disposition_above_max_rejected() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 100"):
        AuthoredNpc(id="x", name="X", initial_disposition=101)


def test_empty_name_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        AuthoredNpc(id="x", name="")


def test_empty_id_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        AuthoredNpc(id="", name="Sora")


def test_extra_fields_rejected() -> None:
    """`extra='forbid'` catches author typos."""
    with pytest.raises(ValidationError, match="extra"):
        AuthoredNpc.model_validate({"id": "x", "name": "X", "totally_made_up_field": "oops"})
