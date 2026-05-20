"""Schema tests for ConfrontationDef.intent_verbs and .on_intent_mismatch."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import ConfrontationDef


def _minimal_def(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "type": "negotiation",
        "label": "Tense Negotiation",
        "category": "social",
        "player_metric": {"name": "leverage", "threshold": 10},
        "opponent_metric": {"name": "patience", "threshold": 10},
        "beats": [{"id": "haggle", "label": "Haggle", "kind": "push", "stat_check": "cha"}],
    }
    base.update(extra)
    return base


def test_on_intent_mismatch_defaults_to_warn() -> None:
    cdef = ConfrontationDef.model_validate(_minimal_def())
    assert cdef.on_intent_mismatch == "warn"


def test_on_intent_mismatch_accepts_three_values() -> None:
    for value in ("warn", "soft_suggest", "reprompt"):
        cdef = ConfrontationDef.model_validate(_minimal_def(on_intent_mismatch=value))
        assert cdef.on_intent_mismatch == value


def test_on_intent_mismatch_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError) as exc:
        ConfrontationDef.model_validate(_minimal_def(on_intent_mismatch="ignore"))
    assert "on_intent_mismatch" in str(exc.value)


def test_intent_verbs_defaults_to_none() -> None:
    cdef = ConfrontationDef.model_validate(_minimal_def())
    assert cdef.intent_verbs is None


def test_intent_verbs_accepts_list_of_strings() -> None:
    cdef = ConfrontationDef.model_validate(
        _minimal_def(intent_verbs=["haggle", "bargain", "barter"])
    )
    assert cdef.intent_verbs == ["haggle", "bargain", "barter"]


def test_intent_verbs_rejects_non_string_elements() -> None:
    with pytest.raises(ValidationError) as exc:
        ConfrontationDef.model_validate(_minimal_def(intent_verbs=["bargain", 5]))
    assert "intent_verbs" in str(exc.value)
